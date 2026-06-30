from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('appraisals', '0005_backfill_selfreviewbullets'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='selfreviewitem',
            name='descriptor',
        ),
        migrations.RemoveField(
            model_name='selfreviewitem',
            name='met',
        ),
    ]
