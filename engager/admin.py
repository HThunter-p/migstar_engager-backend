from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils import timezone
from django.contrib import messages
from .models import User, PlatformSettings, SubscriptionLevel, CampaignBudget, EngagerQuestion, Campaign, PaymentRequest, Task, TaskClaim, WalletLedger, WithdrawalRequest

@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    fieldsets = DjangoUserAdmin.fieldsets + (("MIGSTAR ENGAGER", {"fields": ("phone","mpesa_number","roles","referral_code","referred_by","creator_active","engager_active","selected_level","fund_password_hash")}),)
    list_display = ("username","email","creator_active","engager_active","is_staff")
    list_filter = ("creator_active","engager_active","is_staff")

@admin.register(PlatformSettings)
class PlatformSettingsAdmin(admin.ModelAdmin):
    list_display = ("creator_activation_fee","free_campaign_slots","referral_percent","withdrawal_threshold","default_task_seconds","updated_at")
    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser
    def has_add_permission(self, request):
        return request.user.is_superuser
    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

@admin.register(SubscriptionLevel)
class SubscriptionLevelAdmin(admin.ModelAdmin):
    list_display = ("name","fee","sample_daily_earning","sample_30_day_earning","sample_60_day_earning","is_active","sort_order")
    list_editable = ("is_active","sort_order")
    def has_add_permission(self, request):
        return request.user.is_superuser
    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser
    def has_delete_permission(self, request, obj=None):
        return request.user.is_superuser

@admin.register(CampaignBudget)
class CampaignBudgetAdmin(admin.ModelAdmin):
    list_display = ("platform","objective","quantity","amount","validity_days","is_active")
    list_filter = ("platform","objective","is_active")

@admin.register(EngagerQuestion)
class EngagerQuestionAdmin(admin.ModelAdmin):
    list_display = ("platform","question","is_active")
    list_filter = ("platform","is_active")
    search_fields = ("question",)

@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("id","creator","platform","objective","title","budget","approved_budget","status","created_at")
    list_filter = ("platform","objective","status")
    search_fields = ("title","url","creator__username")
    readonly_fields = ("created_at","reviewed_at")
    actions = ["approve_campaigns","reject_campaigns"]
    @admin.action(description="Approve selected campaigns")
    def approve_campaigns(self, request, queryset):
        approved_count = 0
        skipped_count = 0
        for c in queryset:
            if not c.is_free_trial and not PaymentRequest.objects.filter(
                campaign=c,
                request_type="CAMPAIGN_BUDGET",
                status="APPROVED",
            ).exists():
                skipped_count += 1
                continue
            c.status = "APPROVED"
            c.reviewed_at = timezone.now()
            c.save(update_fields=["status","reviewed_at"])
            if c.is_free_trial:
                c.activate_subscription()
            approved_count += 1
        if approved_count:
            self.message_user(request, f"Approved {approved_count} campaign(s).", messages.SUCCESS)
        if skipped_count:
            self.message_user(
                request,
                f"Skipped {skipped_count} paid campaign(s) because payment has not been verified.",
                messages.WARNING,
            )
    @admin.action(description="Reject selected campaigns")
    def reject_campaigns(self, request, queryset):
        queryset.update(status="REJECTED", reviewed_at=timezone.now())

@admin.register(PaymentRequest)
class PaymentRequestAdmin(admin.ModelAdmin):
    list_display = ("id","user","request_type","amount","transaction_code","status","created_at")
    list_filter = ("request_type","status")
    search_fields = ("user__username","transaction_code")
    fields = (
        "user","request_type","amount","transaction_code","evidence_note",
        "evidence_preview","admin_payment_details","level","campaign",
        "status","admin_note","created_at","reviewed_at",
    )
    readonly_fields = (
        "user","request_type","amount","transaction_code","evidence_note",
        "evidence_preview","admin_payment_details","level","campaign","status",
        "created_at","reviewed_at",
    )
    actions = ["approve_requests","reject_requests"]
    def has_add_permission(self, request):
        return False
    @admin.display(description="Payment screenshot")
    def evidence_preview(self, obj):
        return "Available in the authenticated admin dashboard." if obj.evidence_screenshot else "No screenshot submitted."
    @admin.action(description="Approve selected payment requests with evidence")
    def approve_requests(self, request, queryset):
        approved_count = 0
        for item in queryset.filter(status="UNDER_REVIEW").exclude(transaction_code="").exclude(evidence_screenshot=""):
            item.status = "APPROVED"
            item.reviewed_at = timezone.now()
            item.save(update_fields=["status","reviewed_at"])
            if item.request_type == "CREATOR_ACTIVATION":
                item.user.creator_active = True
                item.user.save(update_fields=["creator_active"])
            elif item.request_type == "ENGAGER_LEVEL" and item.level_id:
                item.user.selected_level = item.level
                item.user.engager_active = True
                item.user.save(update_fields=["selected_level","engager_active"])
            elif item.request_type == "CAMPAIGN_BUDGET" and item.campaign_id:
                item.campaign.activate_subscription()
            item.evidence_screenshot.delete(save=False)
            item.evidence_screenshot = ""
            item.save(update_fields=["evidence_screenshot"])
            approved_count += 1
        if approved_count:
            self.message_user(request, f"Approved {approved_count} evidenced payment request(s).", messages.SUCCESS)
        if approved_count < queryset.count():
            self.message_user(request, "Some selected requests were skipped because they have no submitted evidence.", messages.WARNING)
    @admin.action(description="Reject selected payment requests")
    def reject_requests(self, request, queryset):
        queryset.filter(status__in=("PENDING","AWAITING_PAYMENT","UNDER_REVIEW")).update(status="REJECTED", reviewed_at=timezone.now())

@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("id","campaign","seconds","is_active")
    list_filter = ("is_active",)
@admin.register(TaskClaim)
class TaskClaimAdmin(admin.ModelAdmin):
    list_display = ("id","task","user","status","claimed_at","submitted_at")
    list_filter = ("status",)
    actions = ["approve_claims","reject_claims"]
    @admin.action(description="Approve selected claims (no reward amount is automatically issued)")
    def approve_claims(self, request, queryset):
        queryset.filter(status="SUBMITTED").update(status="APPROVED")
    @admin.action(description="Reject selected claims")
    def reject_claims(self, request, queryset):
        queryset.filter(status="SUBMITTED").update(status="REJECTED")

@admin.register(WalletLedger)
class WalletLedgerAdmin(admin.ModelAdmin):
    list_display = ("user","entry_type","amount","reference","description","created_at")
    readonly_fields = ("created_at",)
@admin.register(WithdrawalRequest)
class WithdrawalAdmin(admin.ModelAdmin):
    list_display = ("user","amount","mpesa_number","status","created_at")
    list_filter = ("status",)
