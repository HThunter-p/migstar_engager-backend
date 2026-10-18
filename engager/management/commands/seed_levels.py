from django.core.management.base import BaseCommand
from engager.models import SubscriptionLevel, PlatformSettings

class Command(BaseCommand):
    help = "Create default prototype settings and the original demonstration levels."
    def handle(self, *args, **kwargs):
        PlatformSettings.current()
        levels = [
            ("L1",450,16,480,960,1),
            ("L2",1000,35,1050,2100,2),
            ("L3",7000,250,7500,15000,3),
            ("L4",16000,550,16500,33000,4),
            ("L5",20000,700,21000,42000,5),
        ]
        for name, fee, daily, m30, m60, order in levels:
            SubscriptionLevel.objects.update_or_create(name=name, defaults={
                "fee":fee,"sample_daily_earning":daily,"sample_30_day_earning":m30,
                "sample_60_day_earning":m60,"sort_order":order,"is_active":True,
                "description":"Demonstration values editable by platform admin; not guaranteed earnings."
            })
        self.stdout.write(self.style.SUCCESS("Default settings and demo levels are ready."))
