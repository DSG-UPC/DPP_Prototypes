from django.db import models


class TimingSample(models.Model):
    """
    One timed measurement, POSTed by neocyclehub/neocyclehub-bootstrap —
    it lives in its own service with its own volume rather than inside neocyclehub's
    own database: `docker compose down -v` already wipes neocyclehub's
    volume, and doing that shouldn't also erase the measurement history.
    """
    OPERATION_CHOICES = [
        ('issue_dpp', 'Issue DPP'),
        ('issue_dte', 'Issue DTE'),
        ('issue_dcc', 'Issue DCC'),
        ('issue_dfr', 'Issue DFR'),
        ('issue_dia', 'Issue DIA'),
        ('verify_chain', 'Verify provenance chain'),
    ]
    MODE_CHOICES = [
        ('passive', 'Passive (real UI action)'),
        ('benchmark', 'Benchmark (controlled, repeated)'),
    ]

    operation = models.CharField(max_length=30, choices=OPERATION_CHOICES)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default='passive')
    source = models.CharField(max_length=50, help_text="Which app sent this — e.g. neocyclehub / neocyclehub-bootstrap")
    cred_type = models.CharField(max_length=10, blank=True, help_text="DPP/DTE/DCC/DFR/DIA — blank for chain-verification samples")
    actor_name = models.CharField(max_length=250, blank=True)
    label = models.CharField(max_length=300, blank=True, help_text="Human-readable label of what was timed, e.g. the product/credential name")
    chain_depth = models.PositiveIntegerField(null=True, blank=True, help_text="Max hop depth — verify_chain samples only")
    company_count = models.PositiveIntegerField(null=True, blank=True, help_text="Distinct actors in the walked chain — verify_chain samples only")
    chain_position = models.CharField(
        max_length=10, blank=True,
        choices=[('root', 'Root (no upstream)'), ('mid', 'Mid-chain'), ('end', 'End of chain (deepest)')],
        help_text="Which credential a verify_chain sample started from — cost scales entirely with this, "
                   "not just with the network. Blank for passive samples (arbitrary click target, not one "
                   "of the three deliberately-chosen benchmark points).",
    )
    duration_ms = models.FloatField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-recorded_at']

    def __str__(self):
        return f"{self.operation} ({self.mode}) — {self.duration_ms:.1f}ms [{self.source}]"
