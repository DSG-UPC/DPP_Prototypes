from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('supply_chain', '0002_add_country_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='processingevent',
            name='destination_facility_id',
            field=models.URLField(blank=True, help_text='MoveEvent: toFacility URL'),
        ),
        migrations.AddField(
            model_name='processingevent',
            name='destination_facility_name',
            field=models.CharField(blank=True, max_length=250, help_text='MoveEvent: toFacility name'),
        ),
        migrations.AlterField(
            model_name='processingevent',
            name='event_type',
            field=models.CharField(
                choices=[('MakeEvent', 'Make / Produce'), ('ModifyEvent', 'Modify / Transform'), ('MoveEvent', 'Move / Transport')],
                default='MakeEvent',
                max_length=50,
            ),
        ),
    ]
