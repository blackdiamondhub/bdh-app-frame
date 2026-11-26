from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("frame", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="tv",
            name="uid",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
    ]
