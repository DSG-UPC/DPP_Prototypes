import uuid
from django.db import models


class Actor(models.Model):
    """A company participating in the NeoCycle supply chain."""
    class Role(models.TextChoices):
        SUPPLIER = 'supplier', 'Supplier'
        UPCYCLER = 'upcycler', 'Upcycler'
        MANUFACTURER = 'manufacturer', 'Manufacturer'
        AUDITOR  = 'auditor',  'Auditor'

    name = models.CharField(max_length=250)
    role = models.CharField(max_length=20, choices=Role.choices)
    did = models.CharField(max_length=500, help_text="did:web identifier for this actor")
    idhub_url = models.URLField(help_text="Base URL of the idhub signing API, e.g. http://idhub:8000/api/v1")
    idhub_token = models.CharField(max_length=500, help_text="Bearer token for idhub API")
    registered_id = models.CharField(max_length=100, blank=True, help_text="e.g. SIRENE number")
    id_scheme_id = models.URLField(blank=True, help_text="e.g. https://vocabulary.uncefact.org/identifierSchemes/SIRENE")
    id_scheme_name = models.CharField(max_length=100, blank=True, help_text="e.g. SIRENE")
    lei_id = models.CharField(max_length=100, blank=True, help_text="GLEIF LEI code")
    website = models.URLField(blank=True)
    country_code = models.CharField(max_length=2, default='FR', help_text="ISO 3166-1 alpha-2 country code")
    country_name = models.CharField(max_length=100, default='France')

    def __str__(self):
        return f"{self.name} ({self.get_role_display()})"

    def issuer_also_known_as(self):
        parties = []
        if self.website and self.registered_id:
            parties.append({
                "type": ["Party"],
                "id": self.website,
                "name": self.name,
                "registeredId": self.registered_id,
                "idScheme": {
                    "type": ["IdentifierScheme"],
                    "id": self.id_scheme_id,
                    "name": self.id_scheme_name,
                }
            })
        if self.lei_id:
            parties.append({
                "type": ["Party"],
                "id": f"https://search.gleif.org/#/record/{self.lei_id}",
                "name": self.name,
                "registeredId": self.lei_id,
                "idScheme": {
                    "type": ["IdentifierScheme"],
                    "id": "https://www.gleif.org/en/about-lei/iso-17442-the-lei-code-structure",
                    "name": "Legal Entity Identifier (LEI)",
                }
            })
        return parties


class ProductBatch(models.Model):
    """A batch of product issued by an Actor with a GTIN or similar identifier."""
    actor = models.ForeignKey(Actor, on_delete=models.CASCADE, related_name='products')
    name = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    gtin = models.CharField(max_length=200, help_text="GS1 GTIN or equivalent product identifier URL")
    model_number = models.CharField(max_length=100, blank=True)
    batch_number = models.CharField(max_length=100, blank=True)
    id_granularity = models.CharField(max_length=50, default='batch')
    cpc_code = models.CharField(max_length=20, blank=True, help_text="UN CPC code")
    cpc_name = models.CharField(max_length=200, blank=True)

    # Upstream process — stored in DPP as characteristics.upstreamProcess (UNTP extension)
    upstream_process_type    = models.CharField(max_length=10, blank=True, choices=[('SSC','SSC'),('ECEX','ECEX')])
    upstream_source_material = models.CharField(max_length=200, blank=True)
    upstream_source_batch    = models.CharField(max_length=100, blank=True)
    upstream_process_date    = models.DateField(null=True, blank=True)

    # Recycled content — stored in DPP as materialProvenance (UNTP standard field)
    recycled_mass_fraction = models.FloatField(null=True, blank=True, help_text="0.0–1.0 fraction of recycled content")

    def __str__(self):
        return f"{self.name} ({self.batch_number})"

    def product_category(self):
        if not self.cpc_code:
            return []
        return [{
            "code": self.cpc_code,
            "name": self.cpc_name,
            "schemeId": "https://unstats.un.org/unsd/classifications/Econ/cpc/",
            "schemeName": "UN Central Product Classification (CPC)",
        }]


class ConformityClaim(models.Model):
    """A third-party conformity assessment (for DCC credentials)."""
    actor = models.ForeignKey(Actor, on_delete=models.CASCADE, related_name='conformity_claims')
    name = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    standard_name = models.CharField(max_length=200, help_text="e.g. R2:2013 Responsible Recycling Standard")
    standard_id = models.URLField(help_text="URL identifying the standard")
    assessor_name = models.CharField(max_length=200)
    assessor_did = models.CharField(max_length=500)
    assessor_level = models.CharField(max_length=50, default='3rdParty')
    assessment_level = models.CharField(max_length=50, default='scheme-cab')
    attestation_type = models.CharField(max_length=50, default='certification')
    valid_from = models.DateField()
    valid_until = models.DateField()

    def __str__(self):
        return f"{self.standard_name} — {self.actor.name}"


class ProcessingEvent(models.Model):
    """A supply chain event (for DTE credentials)."""
    class EventType(models.TextChoices):
        MAKE = 'MakeEvent', 'Make / Produce'
        MODIFY = 'ModifyEvent', 'Modify / Transform'
        MOVE = 'MoveEvent', 'Move / Transport'

    actor = models.ForeignKey(Actor, on_delete=models.CASCADE, related_name='events')
    name = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    event_type = models.CharField(max_length=50, choices=EventType.choices, default=EventType.MAKE)
    event_date = models.DateTimeField()
    activity_code = models.CharField(max_length=50, blank=True, help_text="GS1 CBV BizStep code")
    activity_name = models.CharField(max_length=100, blank=True)
    input_products = models.ManyToManyField(
        ProductBatch, blank=True, related_name='used_in_events',
        help_text="MakeEvent: inputs consumed. ModifyEvent: products before modification. MoveEvent: products being moved."
    )
    output_products = models.ManyToManyField(
        ProductBatch, blank=True, related_name='produced_by_events',
        help_text="MakeEvent: outputs produced. ModifyEvent: products after modification. (unused for MoveEvent)"
    )
    destination_facility_id = models.URLField(blank=True, help_text="MoveEvent: toFacility URL")
    destination_facility_name = models.CharField(max_length=250, blank=True, help_text="MoveEvent: toFacility name")

    def __str__(self):
        return f"{self.name} ({self.actor.name})"


class IssuedCredential(models.Model):
    """A signed UNTP v0.7.0 credential stored and served by NeoCycleHub."""
    class CredType(models.TextChoices):
        DPP = 'DPP', 'Digital Product Passport'
        DTE = 'DTE', 'Digital Traceability Event'
        DCC = 'DCC', 'Digital Conformity Credential'
        DFR = 'DFR', 'Digital Facility Record'
        DIA = 'DIA', 'Digital Identity Anchor'

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    actor = models.ForeignKey(Actor, on_delete=models.CASCADE, related_name='credentials')
    cred_type = models.CharField(max_length=10, choices=CredType.choices)
    label = models.CharField(max_length=300, help_text="Human-readable label, e.g. product name")
    credential = models.JSONField()
    issued_at = models.DateTimeField(auto_now_add=True)
    product = models.ForeignKey(
        ProductBatch, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='credentials'
    )
    event = models.ForeignKey(
        ProcessingEvent, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='credentials'
    )
    conformity_claim = models.ForeignKey(
        ConformityClaim, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='credentials'
    )

    def __str__(self):
        return f"{self.cred_type} — {self.label}"
