from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Actor',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=250)),
                ('role', models.CharField(choices=[('supplier', 'Supplier'), ('upcycler', 'Upcycler')], max_length=20)),
                ('did', models.CharField(help_text='did:web identifier for this actor', max_length=500)),
                ('idhub_url', models.URLField(help_text='Base URL of the idhub signing API, e.g. http://idhub:8000/api/v1')),
                ('idhub_token', models.CharField(help_text='Bearer token for idhub API', max_length=500)),
                ('registered_id', models.CharField(blank=True, help_text='e.g. SIRENE number', max_length=100)),
                ('id_scheme_id', models.URLField(blank=True, help_text='e.g. https://vocabulary.uncefact.org/identifierSchemes/SIRENE')),
                ('id_scheme_name', models.CharField(blank=True, help_text='e.g. SIRENE', max_length=100)),
                ('lei_id', models.CharField(blank=True, help_text='GLEIF LEI code', max_length=100)),
                ('website', models.URLField(blank=True)),
            ],
        ),
        migrations.CreateModel(
            name='ConformityClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=500)),
                ('description', models.TextField(blank=True)),
                ('standard_name', models.CharField(help_text='e.g. R2:2013 Responsible Recycling Standard', max_length=200)),
                ('standard_id', models.URLField(help_text='URL identifying the standard')),
                ('assessor_name', models.CharField(max_length=200)),
                ('assessor_did', models.CharField(max_length=500)),
                ('assessor_level', models.CharField(default='3rdParty', max_length=50)),
                ('assessment_level', models.CharField(default='scheme-cab', max_length=50)),
                ('attestation_type', models.CharField(default='certification', max_length=50)),
                ('valid_from', models.DateField()),
                ('valid_until', models.DateField()),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='conformity_claims', to='supply_chain.actor')),
            ],
        ),
        migrations.CreateModel(
            name='ProductBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=500)),
                ('description', models.TextField(blank=True)),
                ('gtin', models.CharField(help_text='GS1 GTIN or equivalent product identifier URL', max_length=200)),
                ('model_number', models.CharField(blank=True, max_length=100)),
                ('batch_number', models.CharField(blank=True, max_length=100)),
                ('id_granularity', models.CharField(default='batch', max_length=50)),
                ('cpc_code', models.CharField(blank=True, help_text='UN CPC code', max_length=20)),
                ('cpc_name', models.CharField(blank=True, max_length=200)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='products', to='supply_chain.actor')),
            ],
        ),
        migrations.CreateModel(
            name='ProcessingEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=500)),
                ('description', models.TextField(blank=True)),
                ('event_type', models.CharField(choices=[('MakeEvent', 'Make/Collect'), ('ObservationEvent', 'Observe'), ('AggregationEvent', 'Aggregate'), ('TransformationEvent', 'Transform')], default='MakeEvent', max_length=50)),
                ('event_date', models.DateTimeField()),
                ('activity_code', models.CharField(blank=True, help_text='GS1 CBV BizStep code', max_length=50)),
                ('activity_name', models.CharField(blank=True, max_length=100)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='supply_chain.actor')),
                ('input_products', models.ManyToManyField(blank=True, help_text='Products consumed by this event', related_name='used_in_events', to='supply_chain.productbatch')),
                ('output_products', models.ManyToManyField(blank=True, help_text='Products produced by this event', related_name='produced_by_events', to='supply_chain.productbatch')),
            ],
        ),
        migrations.CreateModel(
            name='IssuedCredential',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('cred_type', models.CharField(choices=[('DPP', 'Digital Product Passport'), ('DTE', 'Digital Traceability Event'), ('DCC', 'Digital Conformity Credential'), ('DFR', 'Digital Facility Record'), ('DIA', 'Digital Identity Anchor')], max_length=10)),
                ('label', models.CharField(help_text='Human-readable label, e.g. product name', max_length=300)),
                ('credential', models.JSONField()),
                ('issued_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='credentials', to='supply_chain.actor')),
                ('conformity_claim', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='credentials', to='supply_chain.conformityclaim')),
                ('event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='credentials', to='supply_chain.processingevent')),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='credentials', to='supply_chain.productbatch')),
            ],
        ),
    ]
