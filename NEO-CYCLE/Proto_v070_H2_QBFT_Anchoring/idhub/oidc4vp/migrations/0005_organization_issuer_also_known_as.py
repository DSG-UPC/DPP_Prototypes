from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('oidc4vp', '0004_organization_domain'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='issuer_also_known_as',
            field=models.JSONField(
                blank=True,
                default=list,
                help_text='List of party identifier objects for UNTP issuerAlsoKnownAs (GLEIF LEI, SIRENE, etc.)'
            ),
        ),
    ]
