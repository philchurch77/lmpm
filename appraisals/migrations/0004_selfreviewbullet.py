import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('appraisals', '0003_selfreview_selfreviewitem'),
    ]

    operations = [
        migrations.CreateModel(
            name='SelfReviewBullet',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveSmallIntegerField()),
                ('text', models.TextField()),
                ('score', models.PositiveSmallIntegerField(blank=True, choices=[(1, '1'), (2, '2'), (3, '3')], null=True)),
                ('self_review_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='bullets', to='appraisals.selfreviewitem')),
            ],
            options={
                'ordering': ['order'],
                'constraints': [models.UniqueConstraint(fields=('self_review_item', 'order'), name='unique_bullet_order_per_item')],
            },
        ),
    ]
