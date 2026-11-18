"""
Issue every DIA/DFR/DCC/DPP/DTE credential for every actor.

DPPs and DTEs are issued in dependency order — a ProcessingEvent's DTE is only
issued once every one of its input products already has a signed DPP, so the
DTE's relatedDocument can link to it. Right after an event's DTE is issued,
its output products' DPPs are issued immediately, unblocking any downstream
event that consumes them. This is what makes the relatedDocument chain
resolve end-to-end across actor boundaries (Supplier -> Upcycler -> any
downstream manufacturer), regardless of how many hops are in the chain or
what order a human would otherwise click "Issue" buttons in the UI.

Safe to re-run: already-issued credentials are skipped, so this can be used
both to bootstrap a fresh database and to fill in gaps in one that already
has some credentials issued.

Usage: python manage.py issue_all
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from supply_chain.credential_builders import build_dcc, build_dfr, build_dia, build_dpp, build_dte
from supply_chain.issuer import IssuerError, issue_credential
from supply_chain.models import Actor, ConformityClaim, IssuedCredential, ProcessingEvent, ProductBatch
from supply_chain.registry_client import register_dpp


class Command(BaseCommand):
    help = "Issue all DIA/DFR/DCC/DPP/DTE credentials for all actors, in dependency order."

    def add_arguments(self, parser):
        parser.add_argument('--registrar-did', default='did:web:insee.fr')
        parser.add_argument('--registrar-name', default='INSEE')
        parser.add_argument('--registrar-url', default='https://www.insee.fr')

    def handle(self, *args, **options):
        self.public_url = settings.NEOCYCLEHUB_PUBLIC_URL

        self.stdout.write('== Identity anchors, facility records, conformity credentials ==')
        for actor in Actor.objects.all():
            if not actor.idhub_token:
                self.stdout.write(self.style.WARNING(f"  {actor.name}: no idhub_token configured, skipping DIA/DFR"))
                continue
            self._issue_dia(actor, options)
            self._issue_dfr(actor)
        for claim in ConformityClaim.objects.select_related('actor').all():
            if not claim.actor.idhub_token:
                continue
            self._issue_dcc(claim)

        self.stdout.write('== Product passports and traceability events (dependency order) ==')
        self._issue_chain()

    # ── helpers ──────────────────────────────────────────────────────────

    def _already(self, **kw):
        return IssuedCredential.objects.filter(**kw).exists()

    def _issue_dia(self, actor, options):
        if self._already(actor=actor, cred_type='DIA'):
            return
        schema_name, endpoint, subject = build_dia(
            actor, options['registrar_did'], options['registrar_name'], options['registrar_url']
        )
        try:
            issue_credential(actor=actor, cred_type='DIA', endpoint=endpoint, schema_name=schema_name,
                              credential_subject=subject, label=f"Identity Anchor — {actor.name}")
            self.stdout.write(self.style.SUCCESS(f"  DIA — {actor.name}"))
        except IssuerError as e:
            self.stdout.write(self.style.ERROR(f"  DIA FAILED — {actor.name}: {e}"))

    def _issue_dfr(self, actor):
        if self._already(actor=actor, cred_type='DFR'):
            return
        schema_name, endpoint, subject = build_dfr(actor, {})
        try:
            issue_credential(actor=actor, cred_type='DFR', endpoint=endpoint, schema_name=schema_name,
                              credential_subject=subject, label=subject.get('name', actor.name))
            self.stdout.write(self.style.SUCCESS(f"  DFR — {actor.name}"))
        except IssuerError as e:
            self.stdout.write(self.style.ERROR(f"  DFR FAILED — {actor.name}: {e}"))

    def _issue_dcc(self, claim):
        if self._already(conformity_claim=claim, cred_type='DCC'):
            return
        schema_name, endpoint, subject = build_dcc(claim, self.public_url)
        try:
            issue_credential(actor=claim.actor, cred_type='DCC', endpoint=endpoint, schema_name=schema_name,
                              credential_subject=subject, label=claim.standard_name, conformity_claim=claim)
            self.stdout.write(self.style.SUCCESS(f"  DCC — {claim.actor.name}: {claim.standard_name}"))
        except IssuerError as e:
            self.stdout.write(self.style.ERROR(f"  DCC FAILED — {claim.actor.name}: {e}"))

    def _issue_dpp(self, product):
        if self._already(product=product, cred_type='DPP'):
            return
        if not product.actor.idhub_token:
            self.stdout.write(self.style.WARNING(f"  {product.name}: actor has no idhub_token, skipping"))
            return
        related_docs = []
        for ev_cred in IssuedCredential.objects.filter(event__output_products=product, cred_type='DTE'):
            related_docs.append({
                "linkURL": f"{self.public_url}/credentials/{ev_cred.uuid}/",
                "linkName": ev_cred.label,
                "mediaType": "application/ld+json",
                "linkType": "https://test.uncefact.org/vocabulary/linkTypes/dte",
            })
        schema_name, endpoint, subject = build_dpp(product, self.public_url, related_docs or None)
        try:
            issued = issue_credential(actor=product.actor, cred_type='DPP', endpoint=endpoint, schema_name=schema_name,
                                       credential_subject=subject, label=product.name, product=product)
            register_dpp(product.actor, product, issued, self.public_url)
            self.stdout.write(self.style.SUCCESS(f"  DPP — {product.name} ({product.actor.name})"))
        except IssuerError as e:
            self.stdout.write(self.style.ERROR(f"  DPP FAILED — {product.name}: {e}"))

    def _issue_dte(self, event):
        if self._already(event=event, cred_type='DTE'):
            return
        if not event.actor.idhub_token:
            self.stdout.write(self.style.WARNING(f"  {event.name}: actor has no idhub_token, skipping"))
            return
        schema_name, endpoint, subject = build_dte(event, self.public_url)
        try:
            issue_credential(actor=event.actor, cred_type='DTE', endpoint=endpoint, schema_name=schema_name,
                              credential_subject=subject, label=event.name, event=event)
            self.stdout.write(self.style.SUCCESS(f"  DTE — {event.name} ({event.actor.name})"))
        except IssuerError as e:
            self.stdout.write(self.style.ERROR(f"  DTE FAILED — {event.name}: {e}"))

    def _issue_chain(self):
        all_events = list(ProcessingEvent.objects.prefetch_related('input_products', 'output_products').all())
        done_events = set()
        done_products = set()

        def event_ready(ev):
            return all(p.pk in done_products for p in ev.input_products.all())

        progress = True
        while progress:
            progress = False
            for ev in all_events:
                if ev.pk in done_events or not event_ready(ev):
                    continue
                self._issue_dte(ev)
                done_events.add(ev.pk)
                for p in ev.output_products.all():
                    if p.pk not in done_products:
                        self._issue_dpp(p)
                        done_products.add(p.pk)
                progress = True

        # Products never produced by any event (raw/root materials) still need a DPP.
        for p in ProductBatch.objects.all():
            if p.pk not in done_products:
                self._issue_dpp(p)
                done_products.add(p.pk)

        # Events that never became "ready" (e.g. a dangling input reference) are
        # issued last on a best-effort basis so nothing is silently skipped.
        for ev in all_events:
            if ev.pk not in done_events:
                self.stdout.write(self.style.WARNING(
                    f"  {ev.name}: issuing without full input-DPP coverage (unresolved dependency)"
                ))
                self._issue_dte(ev)
                done_events.add(ev.pk)
                for p in ev.output_products.all():
                    if p.pk not in done_products:
                        self._issue_dpp(p)
                        done_products.add(p.pk)
