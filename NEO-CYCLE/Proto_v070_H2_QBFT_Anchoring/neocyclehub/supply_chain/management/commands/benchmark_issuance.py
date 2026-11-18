"""
Controlled, repeated timing benchmarks for the metrics dashboard. Unlike the passive samples recorded from
real dashboard clicks (which include UI/human think-time as noise), this
runs each operation N times back-to-back, Python-level timing only, so the
metrics-dashboard's median/variance for "benchmark" mode is actually
defensible.

Chain verification is read-only, so it's simply repeated N times against
the deepest/most-connected DPP in the fixtures. Credential issuance is not
read-only — each repeat issues a real credential via idhub — so each one is
deleted immediately after being timed (delete_credential_everywhere(), the
same helper the "Delete" button uses), leaving the database in the same
state it started in.

Usage: python manage.py benchmark_issuance [--repeat N]
"""
from django.core.management.base import BaseCommand

from supply_chain.credential_builders import build_dcc, build_dfr, build_dia, build_dpp, build_dte
from supply_chain.issuer import IssuerError, delete_credential_everywhere, issue_credential
from supply_chain.metrics import send_sample
from supply_chain.models import Actor, ConformityClaim, IssuedCredential, ProcessingEvent, ProductBatch
from supply_chain.views import _build_chain, _chain_stats

import time
from django.conf import settings


class Command(BaseCommand):
    help = "Run controlled, repeated timing benchmarks for issuance and chain verification."

    def add_arguments(self, parser):
        parser.add_argument('--repeat', type=int, default=5, help="Repetitions per operation (default 5)")

    def handle(self, *args, **options):
        self.public_url = settings.NEOCYCLEHUB_PUBLIC_URL
        n = options['repeat']

        self.stdout.write(f"== Chain verification x{n} ==")
        self._benchmark_chain_verification(n)

        self.stdout.write(f"== Credential issuance x{n} per type ==")
        self._benchmark_dpp(n)
        self._benchmark_dte(n)
        self._benchmark_dcc(n)
        self._benchmark_dfr(n)
        self._benchmark_dia(n)

        self.stdout.write(self.style.SUCCESS(
            "Done. View results at the metrics-dashboard service (mode=benchmark)."
        ))

    # ── chain verification (read-only, no cleanup needed) ────────────────

    def _benchmark_chain_verification(self, n):
        """
        Chain-verification cost depends entirely on which credential you
        start from — a root credential with no relatedDocument does one
        signature check and stops; the deepest end-of-chain credential
        walks every node behind it. Benchmarking only the deepest chain gives the worst case only — test all three positions so
        the dashboard can show the real range, not one upper-bound number.
        """
        targets = [
            ('root', IssuedCredential.objects.filter(
                cred_type='DPP', product__upstream_source_material=''
            ).select_related('actor').first()),
            ('mid', IssuedCredential.objects.filter(
                cred_type='DPP', label__icontains='Nd-Rich Separated Stream'
            ).select_related('actor').first()),
            ('end', IssuedCredential.objects.filter(
                cred_type='DPP', label__icontains='Fe-Nd Ammonia'
            ).select_related('actor').first()),
        ]

        for position, cred in targets:
            if not cred:
                self.stdout.write(self.style.WARNING(f"  {position}: no matching DPP found. Skipping."))
                continue
            self.stdout.write(f"  {position} — {cred.label} ({cred.actor.name})")
            for i in range(n):
                start = time.perf_counter()
                chain = _build_chain(cred)
                duration_ms = (time.perf_counter() - start) * 1000
                depth, companies = _chain_stats(chain) if chain else (0, set())
                send_sample(
                    "verify_chain", duration_ms, mode="benchmark",
                    cred_type=cred.cred_type, actor_name=cred.actor.name, label=cred.label,
                    chain_depth=depth, company_count=len(companies), chain_position=position,
                )
                self.stdout.write(f"    run {i + 1}/{n}: {duration_ms:.1f}ms, depth={depth}, companies={len(companies)}")

    # ── issuance (mutates state — issue, time, delete, repeat) ────────────

    def _benchmark_dpp(self, n):
        product = ProductBatch.objects.filter(actor__idhub_token__gt='').select_related('actor').first()
        if not product:
            self.stdout.write(self.style.WARNING("  DPP: no product with a token-bearing actor found. Skipping."))
            return
        self.stdout.write(f"  DPP — {product.name} ({product.actor.name})")
        for i in range(n):
            schema_name, endpoint, subject = build_dpp(product, self.public_url)
            try:
                issued = issue_credential(
                    actor=product.actor, cred_type='DPP', endpoint=endpoint, schema_name=schema_name,
                    credential_subject=subject, label=product.name, product=product, metrics_mode='benchmark',
                )
                delete_credential_everywhere(issued)
                self.stdout.write(f"    run {i + 1}/{n} ok")
            except IssuerError as e:
                self.stdout.write(self.style.ERROR(f"    run {i + 1}/{n} FAILED: {e}"))

    def _benchmark_dte(self, n):
        event = ProcessingEvent.objects.filter(actor__idhub_token__gt='').select_related('actor').first()
        if not event:
            self.stdout.write(self.style.WARNING("  DTE: no event with a token-bearing actor found. Skipping."))
            return
        self.stdout.write(f"  DTE — {event.name} ({event.actor.name})")
        for i in range(n):
            schema_name, endpoint, subject = build_dte(event, self.public_url)
            try:
                issued = issue_credential(
                    actor=event.actor, cred_type='DTE', endpoint=endpoint, schema_name=schema_name,
                    credential_subject=subject, label=event.name, event=event, metrics_mode='benchmark',
                )
                delete_credential_everywhere(issued)
                self.stdout.write(f"    run {i + 1}/{n} ok")
            except IssuerError as e:
                self.stdout.write(self.style.ERROR(f"    run {i + 1}/{n} FAILED: {e}"))

    def _benchmark_dcc(self, n):
        claim = ConformityClaim.objects.filter(actor__idhub_token__gt='').select_related('actor').first()
        if not claim:
            self.stdout.write(self.style.WARNING("  DCC: no claim with a token-bearing actor found. Skipping."))
            return
        self.stdout.write(f"  DCC — {claim.standard_name} ({claim.actor.name})")
        for i in range(n):
            schema_name, endpoint, subject = build_dcc(claim, self.public_url)
            try:
                issued = issue_credential(
                    actor=claim.actor, cred_type='DCC', endpoint=endpoint, schema_name=schema_name,
                    credential_subject=subject, label=claim.standard_name, conformity_claim=claim, metrics_mode='benchmark',
                )
                delete_credential_everywhere(issued)
                self.stdout.write(f"    run {i + 1}/{n} ok")
            except IssuerError as e:
                self.stdout.write(self.style.ERROR(f"    run {i + 1}/{n} FAILED: {e}"))

    def _benchmark_dfr(self, n):
        actor = Actor.objects.exclude(role='auditor').filter(idhub_token__gt='').first()
        if not actor:
            self.stdout.write(self.style.WARNING("  DFR: no token-bearing actor found. Skipping."))
            return
        self.stdout.write(f"  DFR — {actor.name}")
        for i in range(n):
            schema_name, endpoint, subject = build_dfr(actor, {})
            try:
                issued = issue_credential(
                    actor=actor, cred_type='DFR', endpoint=endpoint, schema_name=schema_name,
                    credential_subject=subject, label=subject.get('name', actor.name), metrics_mode='benchmark',
                )
                delete_credential_everywhere(issued)
                self.stdout.write(f"    run {i + 1}/{n} ok")
            except IssuerError as e:
                self.stdout.write(self.style.ERROR(f"    run {i + 1}/{n} FAILED: {e}"))

    def _benchmark_dia(self, n):
        actor = Actor.objects.exclude(role='auditor').filter(idhub_token__gt='').first()
        if not actor:
            self.stdout.write(self.style.WARNING("  DIA: no token-bearing actor found. Skipping."))
            return
        self.stdout.write(f"  DIA — {actor.name}")
        for i in range(n):
            schema_name, endpoint, subject = build_dia(actor, '', '', '')
            try:
                issued = issue_credential(
                    actor=actor, cred_type='DIA', endpoint=endpoint, schema_name=schema_name,
                    credential_subject=subject, label=f"Identity Anchor — {actor.name}", metrics_mode='benchmark',
                )
                delete_credential_everywhere(issued)
                self.stdout.write(f"    run {i + 1}/{n} ok")
            except IssuerError as e:
                self.stdout.write(self.style.ERROR(f"    run {i + 1}/{n} FAILED: {e}"))
