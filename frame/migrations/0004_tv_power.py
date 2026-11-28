from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("frame", "0003_placement_framed"),
    ]

    operations = [
        migrations.AddField(
            model_name="tv",
            name="ip_token",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="tv",
            name="mac",
            field=models.CharField(blank=True, max_length=17),
        ),
        migrations.AddField(
            model_name="tv",
            name="asleep",
            field=models.BooleanField(default=False),
        ),
    ]
