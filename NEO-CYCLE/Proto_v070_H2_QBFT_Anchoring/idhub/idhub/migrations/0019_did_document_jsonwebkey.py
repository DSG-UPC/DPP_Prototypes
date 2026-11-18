import json

from django.db import migrations


OLD_TYPE = "Ed25519VerificationKey2018"
NEW_TYPE = "JsonWebKey"
NEW_TERM_IRI = "https://w3id.org/security#JsonWebKey"


def to_jsonwebkey(apps, schema_editor):
    # VC-JOSE-COSE verifiers (e.g. the UNTP Playground) only accept a
    # publicKeyJwk verification method of type JsonWebKey for vc+jwt.
    # Rewrites the stored DID documents; keys are unchanged.
    DID = apps.get_model("idhub", "DID")
    for did in DID.objects.exclude(didweb_document__isnull=True).exclude(didweb_document=""):
        try:
            doc = json.loads(did.didweb_document)
        except json.JSONDecodeError:
            continue
        changed = False
        for vm in doc.get("verificationMethod", []):
            if vm.get("type") == OLD_TYPE and "publicKeyJwk" in vm:
                vm["type"] = NEW_TYPE
                changed = True
        for ctx in doc.get("@context", []):
            if isinstance(ctx, dict) and OLD_TYPE in ctx:
                del ctx[OLD_TYPE]
                ctx[NEW_TYPE] = NEW_TERM_IRI
                changed = True
        if changed:
            did.didweb_document = json.dumps(doc)
            did.save(update_fields=["didweb_document"])


class Migration(migrations.Migration):

    dependencies = [
        ("idhub", "0018_schemas_validation_url"),
    ]

    operations = [
        migrations.RunPython(to_jsonwebkey, migrations.RunPython.noop),
    ]
