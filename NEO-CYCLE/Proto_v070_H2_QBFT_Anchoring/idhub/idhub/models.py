import os
import json
import ujson
import pytz
import hashlib
import logging
import datetime
import requests
import jwt
from jwt.algorithms import get_default_algorithms
from collections import OrderedDict
from urllib.parse import urljoin, urlparse
from django.db import models
from django.conf import settings
from django.urls import reverse
from django.template.loader import get_template
from django.utils.translation import gettext_lazy as _
from pyvckit.did import (
    generate_keys,
    generate_did,
    gen_did_document,
)
from pyvckit.sign import sign
from idhub.credential_renderer import generate_universal_template
from pyvckit.verify import verify_signature, verify_schema

from oidc4vp.models import Organization
from idhub_auth.models import User


logger = logging.getLogger(__name__)


# terms protected by https://www.w3.org/2018/credentials/v1
TERMS_PROTECTED = [
    'id',
    'type',
    'VerifiableCredential',
    'VerifiablePresenture2019',
    'EcdsaSecp256r1Signature2019',
    'Ed25519Signature2018',
    'RsaSignature2018',
    'proof'
]


def sign_jose_vc(vc, issuer_did, key_material):
    """
    Sign a Verifiable Credential dict as a compact JWS (EdDSA), per the W3C
    VC-JOSE-COSE enveloping proof mechanism UNTP v0.7.0 requires. Returns the
    compact JWT string (header.payload.signature).
    """
    try:
        eddsa_alg = get_default_algorithms()["EdDSA"]
        private_key = eddsa_alg.from_jwk(key_material)
    except Exception as e:
        raise ValueError(f"Failed to parse Ed25519 JWK: {e}")

    # VC Data Model 2.0 + VC-JOSE-COSE (application/vc+jwt): the JWT claims
    # set IS the credential. Until 2026-10-07 this used the VC 1.1 JWT
    # encoding instead ({"jti", "iss", "nbf", "iat", "exp", "vc": {...}}),
    # which UNTP validators such as the UNTP Playground cannot read: they find
    # no "type" at the top level of the payload. Validity now comes from the
    # credential's own validFrom/validUntil (checked in verify_jose_vc).
    jwt_payload = vc

    headers = {
        "typ": "vc+jwt",
        "cty": "vc",
        "kid": f"{issuer_did}#owner"
    }

    return jwt.encode(jwt_payload, private_key, algorithm="EdDSA", headers=headers)


def verify_jose_vc(envelope):
    """
    Verify a JOSE-enveloped Verifiable Credential (EnvelopedVerifiableCredential),
    the counterpart to sign_jose_vc(). Resolves the issuer DID from idhub's
    local registry (no outbound HTTP, so demo DIDs work correctly, same as
    the Data Integrity verifier this mirrors). Returns (valid, detail).
    """
    data_uri = envelope.get("id", "")
    if "data:application/vc+jwt," not in data_uri:
        return False, "Missing data:application/vc+jwt, URI"
    token = data_uri.split("data:application/vc+jwt,", 1)[1]

    try:
        header = jwt.get_unverified_header(token)
    except Exception as e:
        return False, f"Could not parse JWT header: {e}"

    kid = header.get("kid", "")
    did_str = kid.split("#")[0]
    if not did_str:
        return False, "JWT header has no 'kid' to resolve the issuer DID from"

    did_obj = DID.objects.filter(did=did_str).first()
    if not did_obj or not did_obj.didweb_document:
        return False, f"DID not registered in this idhub: {did_str}"

    try:
        did_doc = json.loads(did_obj.didweb_document)
        x = did_doc["verificationMethod"][0]["publicKeyJwk"]["x"]
        eddsa_alg = get_default_algorithms()["EdDSA"]
        public_key = eddsa_alg.from_jwk(json.dumps({"kty": "OKP", "crv": "Ed25519", "x": x}))
    except Exception as e:
        return False, f"Could not extract public key from DID document: {e}"

    try:
        payload = jwt.decode(token, public_key, algorithms=["EdDSA"], options={"verify_aud": False})
    except jwt.exceptions.InvalidSignatureError:
        return False, "Cryptographic signature verification failed (Bad Signature)"
    except jwt.exceptions.ExpiredSignatureError:
        return False, "Credential has expired"
    except Exception as e:
        return False, f"JWT verification failed: {e}"

    # VC 2.0 (vc+jwt): the payload is the credential. A "vc" claim means the
    # older VC 1.1 JWT encoding, still accepted here for reading old tokens.
    vc = payload.get("vc", payload)
    issuer = vc.get("issuer")
    issuer_id = issuer.get("id") if isinstance(issuer, dict) else issuer
    if issuer_id != did_str and payload.get("iss") != did_str:
        return False, f"Credential issuer ({issuer_id}) does not match kid-derived DID ({did_str})"

    now = datetime.datetime.now(datetime.timezone.utc)
    for field, too_early in (("validFrom", True), ("validUntil", False)):
        value = vc.get(field)
        if not value:
            continue
        try:
            when = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            return False, f"Credential {field} is not a valid date-time: {value}"
        if too_early and now < when:
            return False, f"Credential is not valid before {value}"
        if not too_early and now > when:
            return False, "Credential has expired"

    status = vc.get("credentialStatus")
    if isinstance(status, dict) and status.get("type") == "BitstringStatusListEntry":
        try:
            index = int(status["statusListIndex"])
            status_did_pk = int(status["statusListCredential"].rstrip("/").rsplit("/", 1)[-1])
        except (KeyError, ValueError, TypeError):
            status_did_pk = None

        if status_did_pk is not None:
            status_did = DID.objects.filter(pk=status_did_pk).first()
            if status_did and is_status_index_revoked(status_did, index):
                return False, f"Credential has been revoked (statusListIndex {index})"

    return True, f"Signature valid (DID: {did_str})"


def compute_status_bitstring(did):
    """
    Raw (uncompressed) W3C Bitstring Status List bytes for one issuer DID:
    one bit per VerificableCredential.id, set = revoked. Minimum 131,072
    bits (16KB) per the spec's herd-privacy requirement. Single source of
    truth shared by ServeBitstringStatusListView (which publishes it,
    GZIP-compressed and signed) and verify_jose_vc (which checks a single
    bit locally, in-process, without an HTTP round trip to itself).
    """
    revoked_ids = list(
        did.vcredentials.filter(status=VerificableCredential.Status.REVOKED)
        .values_list("id", flat=True)
    )
    min_bits = 131072
    highest_needed = max(revoked_ids, default=0) + 1
    size_bits = max(min_bits, ((highest_needed + 7) // 8) * 8)
    bitstring = bytearray(size_bits // 8)
    for index in revoked_ids:
        byte_index, bit_in_byte = divmod(index, 8)
        bitstring[byte_index] |= 0x80 >> bit_in_byte
    return bytes(bitstring)


def is_status_index_revoked(did, index):
    bitstring = compute_status_bitstring(did)
    byte_index, bit_in_byte = divmod(index, 8)
    if byte_index >= len(bitstring):
        return False
    return bool((bitstring[byte_index] >> (7 - bit_in_byte)) & 1)


class Event(models.Model):
    class Types(models.IntegerChoices):
        EV_USR_REGISTERED = 1, "User registered"
        EV_USR_WELCOME = 2, "User welcomed"
        EV_DATA_UPDATE_REQUESTED_BY_USER = 3, "Data update requested by user"
        EV_DATA_UPDATE_REQUESTED = 4, "Data update requested. Pending approval by administrator"
        EV_USR_UPDATED_BY_ADMIN = 5, "User's data updated by admin"
        EV_USR_UPDATED = 6, "Your data updated by admin"
        EV_USR_DELETED_BY_ADMIN = 7, "User deactivated by admin"
        EV_DID_CREATED_BY_USER = 8, "DID created by user"
        EV_DID_CREATED = 9, "DID created"
        EV_DID_DELETED = 10, "DID deleted"
        EV_CREDENTIAL_DELETED_BY_USER = 11, "Credential deleted by user"
        EV_CREDENTIAL_DELETED_BY_ADMIN = 12, "Credential deleted by admin"
        EV_CREDENTIAL_DELETED = 13, "Credential deleted"
        EV_CREDENTIAL_ISSUED_FOR_USER = 14, "Credential issued for user"
        EV_CREDENTIAL_ISSUED = 15, "Credential issued"
        EV_CREDENTIAL_PRESENTED_BY_USER = 16, "Credential presented by user"
        EV_CREDENTIAL_PRESENTED = 17, "Credential presented"
        EV_CREDENTIAL_ENABLED = 18, "Credential enabled"
        EV_CREDENTIAL_CAN_BE_REQUESTED = 19, "Credential enabled"
        EV_CREDENTIAL_REVOKED_BY_ADMIN = 20, "Credential revoked by admin"
        EV_CREDENTIAL_REVOKED = 21, "Credential revoked"
        EV_ROLE_CREATED_BY_ADMIN = 22, "Role created by admin"
        EV_ROLE_MODIFIED_BY_ADMIN = 23, "Role modified by admin"
        EV_ROLE_DELETED_BY_ADMIN = 24, "Role deleted by admin"
        EV_SERVICE_CREATED_BY_ADMIN = 25, "Service created by admin"
        EV_SERVICE_MODIFIED_BY_ADMIN = 26, "Service modified by admin"
        EV_SERVICE_DELETED_BY_ADMIN = 27, "Service deleted by admin"
        EV_ORG_DID_CREATED_BY_ADMIN = 28, "Organisational DID created by admin"
        EV_ORG_DID_DELETED_BY_ADMIN = 29, "Organisational DID deleted by admin"
        EV_USR_DEACTIVATED_BY_ADMIN = 30, "User deactivated"
        EV_USR_ACTIVATED_BY_ADMIN = 31, "User activated"
        EV_USR_SEND_VP = 32, "User send Verificable Presentation"
        EV_USR_SEND_CREDENTIAL = 33, "User send credential"
        EV_SCHEME_UPLOAD = 34, "Upload new Schema"
        EV_USR_CRED_TO_DLT = 35, "User presented a credential to DLT"

    created = models.DateTimeField(_("Date"), auto_now=True)
    message = models.CharField(_("Description"), max_length=350)
    type = models.PositiveSmallIntegerField(
        _("Event"),
        choices=Types.choices,
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='events',
        null=True,
    )

    def get_type_name(self):
        return self.Types(self.type).label

    @classmethod
    def set_EV_USR_REGISTERED(cls, user):
        msg = _("The user {username} was registered: name: {first_name}, last name: {last_name}").format(
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_REGISTERED,
            message=msg
        )

    @classmethod
    def set_EV_USR_WELCOME(cls, user):
        msg = _("Welcome. You has been registered: name: {first_name}, last name: {last_name}").format(
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_WELCOME,
            message=msg,
            user=user
        )

    # Is required?
    @classmethod
    def set_EV_DATA_UPDATE_REQUESTED_BY_USER(cls, user):
        msg = _("The user '{username}' has request the update of the following information: ")
        msg += "['field1':'value1', 'field2':'value2'>,...]"
        msg = msg.format(username=user.username)
        cls.objects.create(
            type=cls.Types.EV_DATA_UPDATE_REQUESTED_BY_USER,
            message=msg,
        )

    # Is required?
    @classmethod
    def set_EV_DATA_UPDATE_REQUESTED(cls, user):
        msg = _("You have requested the update of the following information: ")
        msg += "['field1':'value1', 'field2':'value2'>,...]"
        cls.objects.create(
            type=cls.Types.EV_DATA_UPDATE_REQUESTED,
            message=msg,
            user=user
        )

    @classmethod
    def set_EV_USR_UPDATED_BY_ADMIN(cls, user):
        msg = "The admin has updated the following user 's information: "
        msg += "name: {first_name}, last name: {last_name}"
        msg = _(msg).format(
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_UPDATED_BY_ADMIN,
            message=msg
        )

    @classmethod
    def set_EV_USR_UPDATED(cls, user):
        msg = "The admin has updated your personal information: "
        msg += "name: {first_name}, last name: {last_name}"
        msg = _(msg).format(
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_UPDATED,
            message=msg,
            user=user
        )

    @classmethod
    def set_EV_USR_DELETED_BY_ADMIN(cls, user):
        msg = _("The admin has deleted the user: username: {username}").format(
            username=user.username,
        )
        cls.objects.create(
            type=cls.Types.EV_USR_DELETED_BY_ADMIN,
            message=msg
        )

    @classmethod
    def set_EV_DID_CREATED_BY_USER(cls, did):
        msg = _("New DID with DID-ID: '{did}' created by user '{username}'").format(
            did=did.did,
            username=did.user.username
        )
        cls.objects.create(
            type=cls.Types.EV_DID_CREATED_BY_USER,
            message=msg,
        )

    @classmethod
    def set_EV_DID_CREATED(cls, did):
        msg = _("New DID with label: '{label}' and DID-ID: '{did}' was created'").format(
            label=did.label,
            did=did.did
        )
        cls.objects.create(
            type=cls.Types.EV_DID_CREATED,
            message=msg,
            user=did.user
        )

    @classmethod
    def set_EV_DID_DELETED(cls, did):
        msg = _("The DID with label '{label}' and DID-ID: '{did}' was deleted from your wallet").format(
            label=did.label,
            did=did.did
        )
        cls.objects.create(
            type=cls.Types.EV_DID_DELETED,
            message=msg,
            user=did.user
        )

    @classmethod
    def set_EV_CREDENTIAL_DELETED_BY_ADMIN(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was deleted").format(
            type=cred.type,
            id=cred.id,
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_DELETED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_CREDENTIAL_DELETED(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was deleted from your wallet").format(
            type=cred.type,
            id=cred.id
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_DELETED,
            message=msg,
            user=cred.user
        )

    @classmethod
    def set_EV_CREDENTIAL_ISSUED_FOR_USER(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was issued for user {username}").format(
            type=cred.type,
            id=cred.id,
            username=cred.user.username
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_ISSUED_FOR_USER,
            message=msg,
        )

    @classmethod
    def set_EV_CREDENTIAL_ISSUED(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was issued and stored in your wallet").format(
            type=cred.type,
            id=cred.id
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_ISSUED,
            message=msg,
            user=cred.user
        )

    @classmethod
    def set_EV_CREDENTIAL_PRESENTED_BY_USER(cls, cred, verifier):
        msg = "The credential of type '{type}' and ID: '{id}' "
        msg += "was presented by user {username} to verifier '{verifier}"
        msg = _(msg).format(
            type=cred.type,
            id=cred.id,
            username=cred.user.username,
            verifier=verifier
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_PRESENTED_BY_USER,
            message=msg,
        )

    @classmethod
    def set_EV_CREDENTIAL_PRESENTED(cls, cred, verifier):
        msg = "The credential of type '{type}' and ID: '{id}' "
        msg += "was presented to verifier '{verifier}'"
        msg = _(msg).format(
            type=cred.type,
            id=cred.id,
            verifier=verifier
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_PRESENTED,
            message=msg,
            user=cred.user
        )

    @classmethod
    def set_EV_CREDENTIAL_ENABLED(cls, cred):
        msg = _("The credential of type '{type}' was enabled for user {username}").format(
            type=cred.type,
            username=cred.user.username
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_ENABLED,
            message=msg,
        )

    @classmethod
    def set_EV_CREDENTIAL_CAN_BE_REQUESTED(cls, cred):
        msg = _("You can request the '{type}' credential").format(
            type=cred.type
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_CAN_BE_REQUESTED,
            message=msg,
            user=cred.user
        )

    @classmethod
    def set_EV_CREDENTIAL_REVOKED_BY_ADMIN(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was revoked for ").format(
            type=cred.type,
            id=cred.id
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_REVOKED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_CREDENTIAL_REVOKED(cls, cred):
        msg = _("The credential of type '{type}' and ID: '{id}' was revoked by admin").format(
            type=cred.type,
            id=cred.id
        )
        cls.objects.create(
            type=cls.Types.EV_CREDENTIAL_REVOKED,
            message=msg,
            user=cred.user
        )

    @classmethod
    def set_EV_ROLE_CREATED_BY_ADMIN(cls):
        msg = _('A new role was created by admin')
        cls.objects.create(
            type=cls.Types.EV_ROLE_CREATED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_ROLE_MODIFIED_BY_ADMIN(cls):
        msg = _('The role was modified by admin')
        cls.objects.create(
            type=cls.Types.EV_ROLE_MODIFIED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_ROLE_DELETED_BY_ADMIN(cls):
        msg = _('The role was removed by admin')
        cls.objects.create(
            type=cls.Types.EV_ROLE_DELETED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_SERVICE_CREATED_BY_ADMIN(cls):
        msg = _('A new service was created by admin')
        cls.objects.create(
            type=cls.Types.EV_SERVICE_CREATED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_SERVICE_MODIFIED_BY_ADMIN(cls):
        msg = _('The service was modified by admin')
        cls.objects.create(
            type=cls.Types.EV_SERVICE_MODIFIED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_SERVICE_DELETED_BY_ADMIN(cls):
        msg = _('The service was removed by admin')
        cls.objects.create(
            type=cls.Types.EV_SERVICE_DELETED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_ORG_DID_CREATED_BY_ADMIN(cls, did):
        msg = _("New Organisational DID with label: '{label}' and DID-ID: '{did}' was created").format(
            label=did.label,
            did=did.did
        )
        cls.objects.create(
            type=cls.Types.EV_ORG_DID_CREATED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_ORG_DID_DELETED_BY_ADMIN(cls, did):
        msg = _("Organisational DID with label: '{label}' and DID-ID: '{did}' was removed").format(
            label=did.label,
            did=did.did
        )
        cls.objects.create(
            type=cls.Types.EV_ORG_DID_DELETED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_USR_DEACTIVATED_BY_ADMIN(cls, user):
        msg = "The user '{username}' was temporarily deactivated: "
        msg += "[name:'{first_name}', last name:'{last_name}']"
        msg = _(msg).format(
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_DEACTIVATED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_USR_ACTIVATED_BY_ADMIN(cls, user):
        msg = "The user '{username}' was activated: "
        msg += "name:'{first_name}', last name:'{last_name}']"
        msg = _(msg).format(
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        cls.objects.create(
            type=cls.Types.EV_USR_ACTIVATED_BY_ADMIN,
            message=msg,
        )

    @classmethod
    def set_EV_USR_SEND_VP(cls, msg, user):
        cls.objects.create(
            type=cls.Types.EV_USR_SEND_VP,
            message=msg,
            user=user
        )

    @classmethod
    def set_EV_USR_SEND_CREDENTIAL(cls, msg):
        cls.objects.create(
            type=cls.Types.EV_USR_SEND_CREDENTIAL,
            message=msg,
        )

    @classmethod
    def set_EV_SCHEME_UPLOAD(cls, msg):
        cls.objects.create(
            type=cls.Types.EV_SCHEME_UPLOAD,
        )

    @classmethod
    def set_EV_USR_CRED_TO_DLT(cls, msg):
        cls.objects.create(
            type=cls.Types.EV_USR_CRED_TO_DLT,
            message=msg,
        )


class DID(models.Model):
    class Types(models.IntegerChoices):
        WEB = 1, "Web"
        KEY = 2, "Key"
        WEBETH = 3, "Web+Ether"

    type = models.PositiveSmallIntegerField(
        _("Type"),
        choices=Types.choices,
    )
    created_at = models.DateTimeField(auto_now=True)
    label = models.CharField(_("Label"), max_length=50)
    did = models.CharField(max_length=250)
    # In JWK format. Must be stored as-is and passed whole to library functions.
    # Example key material:
    # '{"kty":"OKP","crv":"Ed25519","x":"oB2cPGFx5FX4dtS1Rtep8ac6B__61HAP_RtSzJdPxqs","d":"OJw80T1CtcqV0hUcZdcI-vYNBN1dlubrLaJa0_se_gU"}'
    key_material = models.TextField()
    eidas1 = models.BooleanField(default=False)
    is_product = models.BooleanField(default=False)
    service_endpoint = models.CharField(_('Service URI endpoint'), blank=True, max_length=250)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='dids',
        null=True,
    )
    # JSON-serialized DID document
    didweb_document = models.TextField()
    available = models.BooleanField(default=True)

    def check_remote_did(self):
        if self.get_type() != self.Types.WEB:
            return True

        sdid = self.did[8:].split(":")
        if len(sdid) > 2:
            url = "://{}/did.json".format("/".join(sdid))
        elif len(sdid) == 2:
            url = "://{}/.well-known/{}/did.json".format(*sdid)
        try:
            try:
                verify = not settings.DEBUG
                response = requests.get("https"+url, verify=verify)
            except Exception:
                response = requests.get("http"+url)

            if 200 <= response.status_code < 300:
                return response.text == self.didweb_document
        except Exception:
            return False

        return False

    @property
    def is_organization_did(self):
        if not self.user:
            return True
        return False

    @property
    def is_web(self):
      typ = self.Types(self.type)
      return typ in [self.Types.WEB, self.Types.WEBETH]

    def get_key_material(self):
        user = self.user or self.get_organization()
        return user.decrypt_data(self.key_material)

    def set_key_material(self, value):
        user = self.user or self.get_organization()
        if not user.encrypted_sensitive_data:
            user.set_encrypted_sensitive_data()
            user.save()
        self.key_material = user.encrypt_data(value)

    def set_did(self, new_key_material=None):
        if not new_key_material:
            new_key_material = generate_keys()

        if self.type == self.Types.KEY:
            self.did = generate_did(new_key_material)
        elif self.is_web:
            url = "https://{}".format(settings.DOMAIN)
            path = reverse("idhub:serve_did", args=["a"])

            if path and ".well-known" not in path:
                path = path.split("/a/did.json")[0]
                url = "https://{}/{}".format(settings.DOMAIN, path)

            if self.type == self.Types.WEBETH:
                register_user_url = f"{settings.API_DLT_URL}/registerUser"
                ether_data = requests.post(register_user_url).json()
                err_token = 'api_token has invalid length'
                err_eth = 'eth_pub_key has invalid length'
                assert len(ether_data['data']['api_token']) == 80, err_token
                assert len(ether_data['data']['eth_pub_key']) == 42, err_eth

                # TODO this is always the same, should be on DLT_INIT
                ether_tao_url = f"{settings.API_DLT_URL}/getTa"
                ether_tao_data = requests.get(ether_tao_url).json()

                ether_chainid_url = f"{settings.API_DLT_URL}/getChainId"
                ether_chainid_data = requests.get(ether_chainid_url).json()

                # convert new_key_material to json to add more elements
                new_key_material_json = json.loads(new_key_material)
                new_key_material_json['eth_api_token'] = ether_data['data']['api_token']
                new_key_material_json['eth_subject_pub_key'] = ether_data['data']['eth_pub_key']
                new_key_material_json['eth_issuer_pub_key'] = ether_tao_data['data']['root_pub_key']
                new_key_material_json['eth_chainid'] = ether_chainid_data['data']['chain_id']
                new_key_material = json.dumps(new_key_material_json)

            did = generate_did(new_key_material, url)
            self.did = ":".join(did.split(":")[0:-1]) + ":" + did[-6:]
            key = json.loads(new_key_material)
            url, self.didweb_document = gen_did_document(self.did, key)

        self.set_key_material(new_key_material)

    def get_did_document(self):
        key = json.loads(self.get_key_material())
        url, self.didweb_document = gen_did_document(self.did, key)

    def get_key(self):
        return json.loads(self.key_material)

    def get_organization(self):
        return Organization.objects.get(main=True)

    def get_path(self):

        didp = self.did.split(":")
        domain = didp[2]
        did_path = didp[3:]

        if settings.DOMAIN == domain:
            if not did_path:
                return reverse("idhub:serve_root_did")
            else:
                path_str = "/".join(did_path)
                if "registry" in self.did:
                    return reverse("idhub:serve_registry_did", args=[path_str])
            return reverse("idhub:serve_did", args=[path_str])

        protocol = "https"
        if not did_path:
            return f"{protocol}://{domain}/.well-known/did.json"

        url_path = "/".join(did_path)
        return f"{protocol}://{domain}/{url_path}/did.json"

    def has_link(self):
        linked_types = [self.Types.WEB, self.Types.WEBETH]
        if self.get_type() in linked_types:
            return True
        return False

    def get_type(self):
        return self.Types(self.type)

    def __str__(self):
        return self.did


class Context(models.Model):
    """
    This context is linked to a schema and is not uploaded when upload a schema.
    Then is autogenerated from the schema
    """
    key = models.CharField(max_length=250, unique=True)

    @classmethod
    def get_context(cls, domain="https://idhub.pangea.org"):
        path = urljoin(domain, "/context/#{}")
        terms = {}
        ctx = {"@context": terms}

        for k in cls.objects.all():
            terms[k.key] = path.format(k.key)

        if not terms:
            # An empty dict is falsy in Python; pyld treats falsy contexts as
            # null (context reset), which fails when protected terms are active.
            # @version 1.1 is a safe no-op that keeps the dict truthy.
            terms["@version"] = 1.1

        return json.dumps(ctx)


class SchemaManager(models.Manager):

    def create(self, **kwargs):
        instancia = super().create(**kwargs)

        if instancia.context:
            return instancia

        for c in instancia.get_schema.get("allOf", []):
            sch_subj = c.get("properties", {}).get("credentialSubject", {})
            if sch_subj:
                for k in sch_subj.get("properties", {}).keys():
                    if k in TERMS_PROTECTED or Context.objects.filter(key=k).exists():
                        continue
                    Context.objects.create(key=k)

        return instancia


class Schemas(models.Model):
    type = models.CharField(max_length=250)
    file_schema = models.CharField(_('Schema'), max_length=250)
    data = models.TextField()
    created_at = models.DateTimeField(_("Date"), auto_now=True)
    _name = models.TextField(_("Name"), null=True, db_column='name')
    _description = models.CharField(_("Description"), max_length=250, null=True, db_column='description')
    template_description = models.TextField(null=True)
    context = models.CharField(_('Context'), null=True, max_length=250)
    validation_url = models.CharField(_('Validation URL'), max_length=500, blank=True, null=True)

    objects = SchemaManager()

    def save(self, *args, **kwargs):
        #calculate validation url given that sometimes it is not onto the $id field in the schema
        if not self.validation_url:
            schema_id = ""
            schema_data = {}

            try:
                if self.data:
                    schema_data = json.loads(self.data)
            except Exception:
                pass

            json_id = schema_data.get("$id", "")
            if json_id and str(json_id).startswith("http"):
                schema_id = str(json_id)

            elif self.file_schema and str(self.file_schema).startswith("http"):
                schema_id = str(self.file_schema)

            elif self.context and str(self.context).startswith("http"):
                context_base = str(self.context)
                if not context_base.endswith('/'):
                    context_base += '/'
                schema_id = urljoin(context_base, str(self.file_schema))

            elif self.file_schema:
                filename = str(self.file_schema)
                schema_id = filename if filename.startswith("/") else f"/schemas/{filename}"

            self.validation_url = schema_id

        super().save(*args, **kwargs)

    @property
    def get_schema(self):
        if not self.data:
            return {}
        return json.loads(self.data)

    def _update_and_get_field(self, field_attr, schema_key, is_json=False):
        field_value = getattr(self, field_attr)
        if not field_value:
            field_value = self.get_schema.get(schema_key, [] if is_json else '')
            self._update_model_field(field_attr, field_value)
        try:
            if is_json:
                return json.loads(field_value)
        except Exception:
            pass

        return field_value

    def _update_model_field(self, field_attr, field_value):
        if field_value:
            setattr(self, field_attr, field_value)
            self.save(update_fields=[field_attr])

    @property
    def url(self):
        return self.validation_url

    @property
    def get_type(self):
        sh = self.get_schema
        return self.is_untp if self.is_untp else sh.get("title", "").title().replace(" ", "")

    @property
    def get_schema_types(self):
        sh = self.get_schema
        properties = sh.get("properties", {})
        type_info = properties.get("type", {}) if isinstance(properties, dict) else {}
        if isinstance(type_info, dict):
            return type_info.get("default", [])
        return []

    @property
    def get_context_uris(self):
        sh = self.get_schema
        if sh:
            return sh.get("properties", "").get("@context", "").get("default", [])

    @property
    def is_untp(self):
        _vc_types = self.get_schema_types
        _untp_types= [
            "DigitalConformityCredential",
            "DigitalProductPassport",
            "DigitalFacilityRecord",
            "DigitalTraceabilityEvent",
            "DigitalIdentityAnchor",
        ]

        _untp_type = next(filter(lambda x: x in _vc_types, _untp_types), None)
        if _untp_type:
            self.type = _untp_type
        return _untp_type

    @property
    def name(self, request=None):
        names = self._update_and_get_field('_name', 'name',
                                           is_json=True)
        language_code = self._get_language_code(request)
        name = self._get_name_by_language(names, language_code)

        return name

    @property
    def has_credentials(self):
        return self.vcredentials.filter(
            status=VerificableCredential.Status.ISSUED).exists()

    def _get_language_code(self, request=None):
        language_code = settings.LANGUAGE_CODE
        if request:
            language_code = request.LANGUAGE_CODE
        if self._is_catalan_code(language_code):
            language_code = 'ca_ES'

        return language_code

    def _get_name_by_language(self, names, lang_code):
        first = {}
        for name in names:
            first = name
            if name.get('lang') == lang_code:
                return name.get('value', "")

        return first.get("value", "")

    def _is_catalan_code(self, language_code):
        return language_code == 'ca'

    @name.setter
    def name(self, value):
        self._name = json.dumps(value)

    @property
    def description(self):
        return self._update_and_get_field('_description', 'description')

    @description.setter
    def description(self, value):
        self._description = value

    def get_credential_subject_schema(self):
        sc = self.get_data()
        properties = sc["allOf"][1]["properties"]["credentialSubject"]["properties"]
        required = sc["allOf"][1]["properties"]["credentialSubject"]["required"]

        if "id" in required:
            required.remove("id")

        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False
        }

        return schema

    def get_data(self):
        return json.loads(self.data)

    def __str__(self):
        return self.file_schema


class ContextFile(models.Model):
    """
    This context is linked to a schema and is uploaded when upload a schema
    """
    data = models.TextField(max_length=250)
    #TODO:could two schemas have same context?
    file_name = models.CharField(max_length=250, unique=False)
    schema = models.ForeignKey(
        Schemas,
        on_delete=models.CASCADE,
        related_name='context_file',
    )


class VCTemplate(models.Model):
    wkit_template_id = models.CharField(max_length=250)
    data = models.TextField()


class VCTemplatePdf(models.Model):
    name = models.CharField(_("Name"), max_length=250, unique=True)
    data = models.FileField(upload_to='pdftemplate/')


class VerificableCredential(models.Model):
    """
        Definition of Verificable Credentials
    """
    class Status(models.IntegerChoices):
        ENABLED = 1, _("Enabled")
        ISSUED = 2, _("Issued")
        REVOKED = 3, _("Revoked")
        EXPIRED = 4, _("Expired")

    type = models.CharField(_("Type"), max_length=250)
    id_string = models.CharField(max_length=250)
    verified = models.BooleanField()
    created_on = models.DateTimeField(auto_now=True)
    issued_on = models.DateTimeField(_("Issued on"), null=True)
    data = models.TextField()
    csv_data = models.TextField()
    json_data = models.JSONField(null=False, default=dict)
    hash = models.CharField(max_length=260)
    subject_id = models.CharField(max_length=250, null=True)
    status = models.PositiveSmallIntegerField(
        _("Status"),
        choices=Status.choices,
        default=Status.ENABLED
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='vcredentials',
        verbose_name=_("User")
    )
    subject_did = models.ForeignKey(
        DID,
        on_delete=models.CASCADE,
        related_name='subject_credentials',
        null=True
    )
    issuer_did = models.ForeignKey(
        DID,
        on_delete=models.CASCADE,
        related_name='vcredentials',
    )
    eidas1_did = models.ForeignKey(
        DID,
        on_delete=models.CASCADE,
        null=True
    )
    template_pdf = models.ForeignKey(
        VCTemplatePdf,
        on_delete=models.CASCADE,
        null=True
    )
    schema = models.ForeignKey(
        Schemas,
        on_delete=models.CASCADE,
        related_name='vcredentials',
    )
    # revocationBitmapIndex = models.AutoField()

    @property
    def is_didweb(self):
        if self.issuer_did.type == DID.Types.WEB.value:
            return True
        return False

    def get_data(self):
        if not self.data:
            return ""

        return self.user.decrypt_data(self.data)

    def set_data(self, value):
        self.data = self.user.encrypt_data(value)

    def get_description(self):
        return self.schema._description or ''

    def is_untp(self):
        return self.schema.is_untp

    def description(self):
        return self.schema.get_schema.get("description")

    def get_type(self, lang=None):
        return self.type

    def get_status(self):
        return self.Status(self.status).label

    def get_datas(self):
        data = self.render()
        credential_subject = ujson.loads(data).get("credentialSubject", {})
        return credential_subject.items()

    @property
    def is_webeth(self):
        return self.issuer_did.type == DID.Types.WEBETH

    def call_oracle(self):

        with open("/shared/" + os.environ["ADMIN_TOKEN_FILE"], "r") as f:
            admin_api_token = f.read().strip()

        oracle_url = f"{settings.API_DLT_URL}/oracle"

        cred = json.loads(self.get_data())

        oracle_payload = {
            'api_token': admin_api_token,
            'Credential': cred
        }
        headers = {'dlt': 'ethereum' }

        response = requests.post(oracle_url, json = oracle_payload, headers = headers).json()

        # mint and allow tokens

        # get operator address and its api_token
        did = self.subject_did
        if did and did.type == DID.Types.WEBETH:
            key_material = json.loads(did.get_key_material())
            operator_api_token = key_material.get('eth_api_token', 'error')
            operator_address = key_material.get('eth_subject_pub_key', 'error')

        mint_url = f"{settings.API_DLT_URL}/mintTokens"
        mint_payload = {
            'api_token': admin_api_token,
            'Address': operator_address
        }
        response = requests.post(mint_url, json = mint_payload, headers = headers).json()

        allow_url = f"{settings.API_DLT_URL}/allowTokens"
        allow_amount = 10000
        allow_payload = {
            'api_token': operator_api_token,
            'Amount': allow_amount
        }

        response = requests.post(allow_url, json = allow_payload, headers = headers).json()

        return response.get('Status') == 'Success'

    def issue(self, did, domain, save=True):
        if self.status == self.Status.ISSUED:
            return

        if did:
            self.subject_did = did
        self.set_issue_date()
        if not self.pk:
            # Needed early so self.id exists as a stable bitstring status
            # list index (some callers issue() before their own save()).
            self.save()
        # hash of credential without sign
        self.hash = hashlib.sha3_256(self.render(domain).encode()).hexdigest()

        verify = not settings.DEBUG
        logger.error(verify)
        logger.error("#######################3")

        if self.is_untp():
            # UNTP v0.7.0 requires VC Data Model 2.0 secured with the JOSE
            # enveloping proof (W3C VC-JOSE-COSE), not the Ed25519Signature2018
            # Data Integrity proof pyvckit.sign.sign() produces. Validate the
            # unsecured credential against its schema, then sign+envelope it
            # as a compact JWS via generate_enveloped_jwt().
            raw_vc_str = self.render_untp(self.is_untp(), domain)
            valid, _detail = verify_schema(raw_vc_str, verify=verify)

            if not valid:
                raise Exception(_("The credential is not valid with this schema"))

            vc_str = self.generate_enveloped_jwt(domain)

            if not settings.DEBUG:
                valid, error = self.verify_enveloped_jwt(vc_str)
                if not valid:
                    raise Exception(_("The credential is not valid"))
        else:
            key = self.issuer_did.get_key_material()
            credential = self.render(domain)

            vc = sign(credential, key, self.issuer_did.did, verify=verify)
            vc_str = json.dumps(vc)

            valid, _detail = verify_schema(vc_str, verify=verify)

            if not valid:
                raise Exception(_("The credential is not valid with this schema"))

            if not settings.DEBUG:
                valid, _detail = verify_signature(vc_str, verify=verify)
                if not valid:
                    raise Exception(_("The credential is not valid"))

        self.data = self.user.encrypt_data(vc_str)

        self.status = self.Status.ISSUED

    def set_issue_date(self):
        if not self.issued_on:
            self.issued_on = datetime.datetime.now().astimezone(pytz.utc)

    def get_context(self, domain):
        d = json.loads(self.csv_data)
        issuance_date = ''
        if self.issued_on:
            format = "%Y-%m-%dT%H:%M:%SZ"
            issuance_date = self.issued_on.strftime(format)

        cred_path = 'credentials'
        sid = self.id or 0
        if self.eidas1_did:
            cred_path = 'public/credentials'
            sid = self.hash

        url_id = "{}/{}/{}".format(
            domain,
            cred_path,
            sid
        )

        org = Organization.objects.get(main=True)

        # TODO support revocation
        credential_status_id = 'https://revocation.not.supported/'
        if self.issuer_did.type == DID.Types.WEB:
            credential_status_id = self.issuer_did.did

        context = {
            'id_credential': str(sid),
            'vc_id': url_id,
            'issuer_did': self.issuer_did.did,
            'subject_did': self.subject_did and self.subject_did.did or '',
            'issuance_date': issuance_date,
            'firstName': self.user.first_name or "",
            'lastName': self.user.last_name or "",
            'email': self.user.email,
            'organisation': org.name or '',
            'credential_status_id': credential_status_id,
            'type': self.schema.get_type
        }

        if self.issuer_did.type == DID.Types.WEBETH:
            issuer_key = json.loads(self.issuer_did.get_key_material())
            context['issuer_id'] = issuer_key['eth_issuer_pub_key']

        if self.subject_did and self.subject_did.type == DID.Types.WEBETH:
            subject_key = json.loads(self.subject_did.get_key_material())
            context['subject_did'] = subject_key['eth_subject_pub_key']

        context.update(d)
        return context

    def get_context_untp(self, domain):
        self.set_issue_date()
        format = "%Y-%m-%dT%H:%M:%SZ"
        issuance_date = self.issued_on.strftime(format)

        cred_path = 'credentials'

        did_parts = self.issuer_did.did.split(":")
        if len(did_parts) >= 3 and did_parts[0] == "did" and did_parts[1] == "web":
            original_domain = did_parts[2]
            protocol = "http" if "localhost" in original_domain else "https"
            base_url = f"{protocol}://{original_domain}"
        else:
            base_url = domain.rstrip('/')


        sid = self.id or 0
        url_id = f"{base_url}/{cred_path}/{sid}"
        org = Organization.objects.get(main=True)

        _vc_type = self.schema.get_schema_types
        _context_urls = self.schema.get_context_uris

        if not isinstance(_context_urls, list):
            _context_urls = [_context_urls] if _context_urls else []

        if self.schema.context:
            schema_ctx = str(self.schema.context)
            if not schema_ctx.startswith("http"):
                schema_ctx = urljoin(base_url, schema_ctx)

            if schema_ctx not in _context_urls:
                _context_urls.append(schema_ctx)
        else:
            local_ctx = urljoin(domain, reverse("idhub:context"))
            if local_ctx not in _context_urls:
                _context_urls.append(local_ctx)

        # JWS / security-v2 contexts are only compatible with VC v1.
        # VC v2 ("credentials/v2") uses @protected terms that conflict with
        # security-v2 redefinitions, causing JSON-LD "invalid context nullification".
        if "https://www.w3.org/ns/credentials/v2" not in _context_urls:
            jws_ctx = "https://w3id.org/security/suites/jws-2020/v1"
            if jws_ctx not in _context_urls:
                _context_urls.append(jws_ctx)

            sec_v2_ctx = "https://w3id.org/security/v2"
            if sec_v2_ctx not in _context_urls:
                _context_urls.append(sec_v2_ctx)

        cred_subject = self.json_data.copy()
        if isinstance(cred_subject, dict):
            cred_subject.pop('@context', None)
        elif isinstance(cred_subject, list):
            cred_subject = [
                {k: v for k, v in item.items() if k != '@context'}
                if isinstance(item, dict) else item
                for item in cred_subject
            ]

        schema_id = ""
        if self.schema.url and str(self.schema.url).startswith("http"):
            schema_id = self.schema.url
        elif self.schema.file_schema and str(self.schema.file_schema).startswith("http"):
            schema_id = str(self.schema.file_schema)
        else:
            schema_id = urljoin(domain, f"/schema/{self.schema.file_schema}")

        issuer_also_known_as = getattr(org, "issuer_also_known_as", None) or []
        credential_name = ""
        if isinstance(self.json_data, dict):
            credential_name = self.json_data.get("name", "")
        elif isinstance(self.json_data, list) and self.json_data:
            credential_name = self.json_data[0].get("name", "")

        valid_until = ""
        if isinstance(self.json_data, dict):
            valid_until = self.json_data.get("validUntil", "")

        context = {
            'context': json.dumps(_context_urls),
            'vc_id': url_id,
            'issuer_did': self.issuer_did.did,
            "issuance_date": issuance_date,
            "valid_until": valid_until,
            "name": getattr(org, "name", "") or "",
            "credential_name": credential_name,
            "issuer_also_known_as": json.dumps(issuer_also_known_as),
            "schema_id": schema_id,
            "subject_id": self.id_string,
            "credential_subject": json.dumps(cred_subject),
            "type": json.dumps(_vc_type)
        }

        if self.issuer_did.type == DID.Types.WEBETH:
            issuer_key = json.loads(self.issuer_did.get_key_material())
            context['issuer_id'] = issuer_key['eth_issuer_pub_key']

        if self.subject_did and self.subject_did.type == DID.Types.WEBETH:
            subject_key = json.loads(self.subject_did.get_key_material())
            context['subject_did'] = subject_key['eth_subject_pub_key']

        return context


    def render_untp(self, untp_type, domain=""):
        context = self.get_context_untp(domain)
        tmpl = get_template('credentials/base_untp.json')
        d_ordered = ujson.loads(tmpl.render(context))

        d_minimum = self.filter_dict(d_ordered)

        if not self.is_didweb:
            d_minimum.pop("credentialStatus", None)

        return ujson.dumps(d_minimum, escape_forward_slashes=False)


    def generate_enveloped_jwt(self, domain=""):
        """
        Experimental: grabs the raw UNTP credential, formats it into a W3C Enveloped VC payload,
        and returns the fully signed JWT string.
        """
        untp_type = self.is_untp()
        if not untp_type:
            raise ValueError("This credential is not a UNTP type.")

        raw_vc_str = self.render_untp(untp_type, domain)
        raw_vc = ujson.loads(raw_vc_str)

        dynamic_html = generate_universal_template(raw_vc)
        # UNTP's RenderTemplate2024 (type is an array). Until 2026-10-07 this
        # was {"type": "WebRenderingTemplate2022", ...}, which the UNTP v0.7.0
        # schema rejects. It went unnoticed because idhub validates the
        # credential before renderMethod is added.
        raw_vc["renderMethod"] = [
            {
                "type": ["RenderTemplate2024"],
                "name": "HTML view",
                "mediaType": "text/html",
                "template": dynamic_html
            }
        ]

        vc_id = raw_vc.get("id", "")

        issuer_obj = raw_vc.get("issuer", {})
        iss = issuer_obj.get("id") if isinstance(issuer_obj, dict) else self.issuer_did.did

        valid_from_str = raw_vc.get("validFrom") or raw_vc.get("issuanceDate")
        if not valid_from_str:
            valid_from_str = self.issued_on.strftime("%Y-%m-%dT%H:%M:%SZ")
            raw_vc["validFrom"] = valid_from_str

        # W3C Bitstring Status List: this credential's revocation bit lives
        # at index self.id in its issuer's status list (same indexing scheme
        # ServeDidView already uses for the legacy RevocationBitmap2022
        # service entry, so both stay consistent with one another).
        parsed_vc_id = urlparse(vc_id)
        base_url = f"{parsed_vc_id.scheme}://{parsed_vc_id.netloc}"
        status_list_url = urljoin(
            base_url, reverse("idhub:bitstring_status_list", args=[self.issuer_did.pk])
        )
        raw_vc["credentialStatus"] = {
            "id": f"{status_list_url}#{self.id}",
            "type": "BitstringStatusListEntry",
            "statusPurpose": "revocation",
            "statusListIndex": self.id,
            "statusListCredential": status_list_url,
        }

        raw_jwk_str = self.issuer_did.get_key_material()
        enveloped_jwt = sign_jose_vc(raw_vc, iss, raw_jwk_str)

        untp_context = self.schema.context if self.schema.context else "https://vocabulary.uncefact.org/untp/0.7.0/context/"

        # Per W3C VC 2.0's enveloping-proof mechanism, the EnvelopedVerifiableCredential
        # object *is* the credential resource — it must not be nested inside another
        # object, or an external UNTP verifier fetching this resource won't recognize it.
        enveloped_vc = {
            "@context": [
                "https://www.w3.org/ns/credentials/v2",
                str(untp_context)
            ],
            "type": "EnvelopedVerifiableCredential",
            "id": f"data:application/vc+jwt,{enveloped_jwt}"
        }

        return ujson.dumps(enveloped_vc, escape_forward_slashes=False)

    def verify_enveloped_jwt(self, vc_str):
        """
        Self-check for the compact JWS produced by generate_enveloped_jwt():
        decode it with the issuer's own public key and confirm the signature,
        'kid' and 'iss' are internally consistent. Mirrors the sanity check
        pyvckit.verify.verify_signature() gives the Data Integrity path, but
        stays local (no DID resolution) since this only needs to catch a
        signing bug right after issuance, not act as an external verifier.
        """
        try:
            envelope = json.loads(vc_str)
            # Accept both the current top-level EnvelopedVerifiableCredential
            # shape and the older {"verifiableCredential": {...}} nesting, so
            # this can still verify credentials issued before that was fixed.
            if "verifiableCredential" in envelope:
                envelope = envelope["verifiableCredential"]
            data_uri = envelope.get("id", "")
            enveloped_jwt = data_uri.split("data:application/vc+jwt,")[-1]
            if not enveloped_jwt or enveloped_jwt == data_uri:
                return False, "Missing data:application/vc+jwt, URI"

            raw_jwk_str = self.issuer_did.get_key_material()
            eddsa_alg = get_default_algorithms()["EdDSA"]
            public_key = eddsa_alg.from_jwk(raw_jwk_str).public_key()

            header = jwt.get_unverified_header(enveloped_jwt)
            if header.get("kid") != f"{self.issuer_did.did}#owner":
                return False, f"Unexpected kid in JWT header: {header.get('kid')}"

            payload = jwt.decode(
                enveloped_jwt,
                public_key,
                algorithms=["EdDSA"],
                options={"verify_aud": False},
            )
            # VC 2.0 vc+jwt: the payload is the credential, so check its issuer.
            vc = payload.get("vc", payload)
            issuer = vc.get("issuer")
            issuer_id = issuer.get("id") if isinstance(issuer, dict) else issuer
            if issuer_id != self.issuer_did.did and payload.get("iss") != self.issuer_did.did:
                return False, f"Credential issuer ({issuer_id}) does not match issuer DID"
        except Exception as e:
            return False, f"Enveloped JWT verification failed: {e}"

        return True, "Success"

    def render(self, domain=""):
        if (_untp_type := self.is_untp()) is not None:
            return self.render_untp(_untp_type, domain)

        context = self.get_context(domain)
        tmpl = get_template('credentials/base.json')
        d_ordered = ujson.loads(tmpl.render(context))
        if self.schema.context:
            d_ordered["@context"].append(self.schema.context)
        else:
            url_context = urljoin(domain, reverse("idhub:context"))
            d_ordered["@context"].append(url_context)
        d_ordered["credentialSchema"]["id"] = self.schema.url
        d_ordered["name"] = self.schema.get_schema.get("name", [])
        d_ordered["description"] = self.schema.get_schema.get("description", [])
        sch_values = []
        for c in self.schema.get_schema.get("allOf", []):
            sch_subj = c.get("properties", {}).get("credentialSubject", {})
            if sch_subj:
                sch_values = sch_subj.get("properties", {}).keys()

        csv_data = json.loads(self.csv_data)
        for k, v in csv_data.items():
            if k in sch_values:
                d_ordered["credentialSubject"][k] = v

        if "evidence" in csv_data:
            d_ordered["evidence"] = csv_data["evidence"].copy()
        d_minimum = self.filter_dict(d_ordered)

        # You can revoke only didweb
        if not self.is_didweb:
            d_minimum.pop("credentialStatus", None)

        return ujson.dumps(d_minimum)

    def get_issued_on(self):
        if self.issued_on:
            return self.issued_on.strftime("%m/%d/%Y")

        return ''

    def set_type(self):
        self.type = self.schema.get_type

    def filter_dict(self, dic):
        #Quotes "", or '' are giving some trouble in jsonschema.validate
        filtered_cases = [False, 0]
        new_dict = OrderedDict()
        for key, value in dic.items():
            if isinstance(value, dict):
                new_value = self.filter_dict(value)
                if new_value:
                    new_dict[key] = new_value
            elif value or value in filtered_cases:
                new_dict[key] = value
        return new_dict


class File_datas(models.Model):
    file_name = models.CharField(_("File"), max_length=250)
    success = models.BooleanField(_("Success"), default=True)
    created_at = models.DateTimeField(_("Date"), auto_now=True)


class Membership(models.Model):
    """
      This model represent the relation of this user with the ecosystem.
    """
    class Types(models.IntegerChoices):
        BENEFICIARY = 1, _('Beneficiary')
        EMPLOYEE = 2, _('Employee')
        MEMBER = 3, _('Member')

    type = models.PositiveSmallIntegerField(_('Type of membership'), choices=Types.choices)
    start_date = models.DateField(
        _('Start date'),
        help_text=_('What date did the membership start?'),
        blank=True,
        null=True
    )
    end_date = models.DateField(
        _('End date'),
        help_text=_('What date will the membership end?'),
        blank=True,
        null=True
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='memberships',
    )

    def get_type(self):
        return dict(self.Types.choices).get(self.type)


class Rol(models.Model):
    name = models.CharField(_("name"), max_length=250)
    description = models.CharField(_("Description"), max_length=250, null=True)

    def __str__(self):
        return self.name


class Service(models.Model):
    domain = models.CharField(_("Domain"), max_length=250)
    description = models.CharField(_("Description"), max_length=250)
    rol = models.ManyToManyField(
        Rol,
    )

    def get_roles(self):
        if self.rol.exists():
            return ", ".join([x.name for x in self.rol.order_by("name")])
        return _("None")

    def __str__(self):
        return "{} -> {}".format(self.domain, self.get_roles())


class UserRol(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='roles',
    )
    service = models.ForeignKey(
        Service,
        verbose_name=_("Service"),
        on_delete=models.CASCADE,
        related_name='users',
    )

    class Meta:
        unique_together = ('user', 'service',)
