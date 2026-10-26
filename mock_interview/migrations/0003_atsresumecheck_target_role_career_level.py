from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("mock_interview", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="atsresumecheck",
            name="target_role",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="atsresumecheck",
            name="career_level",
            field=models.CharField(
                choices=[
                    ("auto", "Auto-detect from resume"),
                    ("fresher", "Fresher"),
                    ("intern", "Intern"),
                    ("experienced", "Experienced"),
                ],
                default="auto",
                max_length=20,
            ),
        ),
    ]
