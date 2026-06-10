from django.db import migrations

DEFAULT_STAGES = [
    ("New", 1, False, False),
    ("Contacted", 2, False, False),
    ("Engaged", 3, False, False),
    ("Qualified", 4, False, False),
    ("Meeting", 5, False, False),
    ("Won", 6, True, False),
    ("Lost", 7, False, True),
]


def seed_stages(apps, schema_editor):
    Stage = apps.get_model("pipeline", "Stage")
    for name, order, is_won, is_lost in DEFAULT_STAGES:
        Stage.objects.get_or_create(
            name=name, defaults={"order": order, "is_won": is_won, "is_lost": is_lost}
        )


def unseed_stages(apps, schema_editor):
    Stage = apps.get_model("pipeline", "Stage")
    Stage.objects.filter(name__in=[s[0] for s in DEFAULT_STAGES], leads__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("pipeline", "0001_initial")]
    operations = [migrations.RunPython(seed_stages, unseed_stages)]
