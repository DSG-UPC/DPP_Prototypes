from django.db import models


class RegistryEntry(models.Model):
    """
    Mock of the EU DPP Registry (single-market-economy.ec.europa.eu/single-market/
    digital-product-passport/dpp-registry_en): a centralized *index* — "unique
    identifiers, registration data, and high-level metadata... rather than the
    full detailed product information contained in the DPP itself." The actual
    DPP content stays hosted by the economic operator (neocyclehub); this only
    stores enough to find it — mirroring the real registry's decentralized-
    content / centralized-index split.
    """
    gtin = models.CharField(max_length=200, help_text="GS1 GTIN or equivalent product identifier URL")
    product_name = models.CharField(max_length=500)
    issuer_did = models.CharField(max_length=500, help_text="Economic operator's did:web")
    issuer_name = models.CharField(max_length=250)
    valid_from = models.CharField(max_length=50, blank=True)
    valid_until = models.CharField(max_length=50, blank=True)
    dpp_url = models.URLField(help_text="Pointer to where the full DPP is actually hosted — not stored here")
    registered_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-registered_at']

    def __str__(self):
        return f"{self.gtin} — {self.product_name} ({self.issuer_name})"
