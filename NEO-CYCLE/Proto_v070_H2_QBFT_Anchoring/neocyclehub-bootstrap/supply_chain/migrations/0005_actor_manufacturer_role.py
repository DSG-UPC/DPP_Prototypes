from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('supply_chain', '0004_productbatch_upstream'),
    ]

    operations = [
        migrations.AlterField(
            model_name='actor',
            name='role',
            field=models.CharField(
                max_length=20,
                choices=[
                    ('supplier', 'Supplier'),
                    ('upcycler', 'Upcycler'),
                    ('manufacturer', 'Manufacturer'),
                    ('auditor', 'Auditor'),
                ],
            ),
        ),
    ]
