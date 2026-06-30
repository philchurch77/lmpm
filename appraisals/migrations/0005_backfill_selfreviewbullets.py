from django.db import migrations


def backfill_bullets(apps, schema_editor):
    """Split each existing item's bullet-blob `descriptor` into individual
    `SelfReviewBullet` rows.

    The old per-group `met` (Yes/No/Not-answered) value is intentionally
    dropped here, not carried forward: there is no valid mapping from one
    group-level boolean onto N independent per-bullet 1-3 scores, so every
    backfilled bullet starts as Not Answered (score=None). `evidence` is
    unaffected — it stays on `SelfReviewItem` and is not touched by this
    migration. Confirmed with the project owner before writing this migration
    that no deployed environment has real self-review answers yet, so this
    drop is not a live-data loss.
    """
    SelfReviewItem = apps.get_model('appraisals', 'SelfReviewItem')
    SelfReviewBullet = apps.get_model('appraisals', 'SelfReviewBullet')

    bullets = []
    for item in SelfReviewItem.objects.all():
        lines = [line.strip().removeprefix('• ').strip() for line in item.descriptor.split('\n')]
        lines = [line for line in lines if line]
        for index, text in enumerate(lines):
            bullets.append(
                SelfReviewBullet(self_review_item=item, order=index + 1, text=text)
            )
    SelfReviewBullet.objects.bulk_create(bullets)


def noop_reverse(apps, schema_editor):
    # Bullets created here are removed by the CreateModel reversal in 0004;
    # nothing additional to undo on this step itself.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('appraisals', '0004_selfreviewbullet'),
    ]

    operations = [
        migrations.RunPython(backfill_bullets, noop_reverse),
    ]
