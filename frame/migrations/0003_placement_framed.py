from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("frame", "0002_tv_uid"),
    ]

    operations = [
        migrations.AddField(
            model_name="placement",
            name="framed",
            field=models.BooleanField(default=False),
        ),
    ]
