"""Add ReasonCode.direction — F23.

The field is added with a one-off default so existing rows do not violate
the NOT NULL constraint (`preserve_default=False`, so the model itself keeps
no default — every future code must choose one deliberately). The data
migration that follows corrects the known seeded codes to their real
direction; only unrecognised codes are left on the placeholder.
"""

from django.db import migrations, models


def set_known_directions(apps, schema_editor):
    ReasonCode = apps.get_model("inventory", "ReasonCode")
    directions = {
        "RET": "INCREASE",
        "XFER": "DECREASE",
        "LOSS": "DECREASE",
        "DMG": "DECREASE",
        # A physical count can go either way in reality; a single code with
        # one fixed direction is a known simplification of F23's first cut,
        # not a considered answer. Revisit when F24 is built.
        "CORR": "DECREASE",
    }
    for code, direction in directions.items():
        ReasonCode.objects.filter(code=code).update(direction=direction)


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0003_warehouse_transfers'),
    ]

    operations = [
        migrations.AddField(
            model_name='reasoncode',
            name='direction',
            field=models.CharField(
                choices=[('INCREASE', 'Increases stock'), ('DECREASE', 'Decreases stock')],
                default='DECREASE',
                help_text='Whether an adjustment posted against this code adds to stock or removes from it.',
                max_length=8,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(set_known_directions, reverse_code=migrations.RunPython.noop),
    ]
