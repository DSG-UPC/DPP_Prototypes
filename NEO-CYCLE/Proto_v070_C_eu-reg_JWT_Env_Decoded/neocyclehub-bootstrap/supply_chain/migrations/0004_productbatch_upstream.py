from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('supply_chain', '0003_processingevent_destination'),
    ]

    operations = [
        migrations.AddField(
            model_name='productbatch',
            name='upstream_process_type',
            field=models.CharField(
                max_length=10, blank=True,
                choices=[('SSC', 'SSC'), ('ECEX', 'ECEX')],
            ),
        ),
        migrations.AddField(
            model_name='productbatch',
            name='upstream_source_material',
            field=models.CharField(max_length=200, blank=True),
        ),
        migrations.AddField(
            model_name='productbatch',
            name='upstream_source_batch',
            field=models.CharField(max_length=100, blank=True),
        ),
        migrations.AddField(
            model_name='productbatch',
            name='upstream_process_date',
            field=models.DateField(null=True, blank=True),
        ),
        migrations.AddField(
            model_name='productbatch',
            name='recycled_mass_fraction',
            field=models.FloatField(
                null=True, blank=True,
                help_text='0.0–1.0 fraction of recycled content',
            ),
        ),
    ]
