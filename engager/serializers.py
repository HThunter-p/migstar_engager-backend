from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers
from .models import User, PlatformSettings, SubscriptionLevel, CampaignBudget, EngagerQuestion, Campaign, PaymentRequest, Task, TaskClaim, WithdrawalRequest, WalletLedger

class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    roles = serializers.ListField(child=serializers.ChoiceField(choices=["CREATOR","ENGAGER"]), allow_empty=False)
    referral_code_input = serializers.CharField(write_only=True, required=False, allow_blank=True)
    class Meta:
        model = User
        fields = ["id","username","email","password","phone","mpesa_number","roles","referral_code_input"]
    def create(self, validated_data):
        referral = validated_data.pop("referral_code_input", "")
        roles = list(dict.fromkeys(validated_data.pop("roles")))
        user = User(**validated_data, roles=roles)
        user.set_password(validated_data["password"])
        if referral:
            user.referred_by = User.objects.filter(referral_code__iexact=referral).first()
        user.save()
        return user

class UserSerializer(serializers.ModelSerializer):
    selected_level_name = serializers.CharField(source="selected_level.name", read_only=True, allow_null=True)
    selected_level_fee = serializers.DecimalField(source="selected_level.fee", max_digits=10, decimal_places=2, read_only=True, allow_null=True)
    class Meta:
        model = User
        fields = ["id","username","email","phone","mpesa_number","roles","referral_code","creator_active","engager_active","selected_level_id","selected_level_name","selected_level_fee","is_staff","is_superuser"]

class SettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlatformSettings
        fields = ["creator_activation_fee","free_campaign_slots","referral_percent","withdrawal_threshold","default_task_seconds","payment_instructions"]

    def validate_withdrawal_threshold(self, value):
        if value <= 0:
            raise serializers.ValidationError("Withdrawal threshold must be greater than zero.")
        return value

class LevelSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionLevel
        fields = ["id","name","fee","sample_daily_earning","sample_30_day_earning","sample_60_day_earning","description","is_active","sort_order"]

class CampaignBudgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = CampaignBudget
        fields = ["id","platform","objective","quantity","amount","validity_days","is_active","created_at"]
        read_only_fields = ["created_at"]

    def validate(self, attrs):
        for field in ("quantity", "validity_days"):
            value = attrs.get(field, getattr(self.instance, field, None))
            if value is not None and value < 1:
                raise serializers.ValidationError({field: "Must be greater than zero."})
        amount = attrs.get("amount", getattr(self.instance, "amount", None))
        if amount is not None and amount <= 0:
            raise serializers.ValidationError({"amount": "Amount must be greater than zero."})
        return attrs

class EngagerQuestionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EngagerQuestion
        fields = ["id","platform","question","is_active","created_at"]
        read_only_fields = ["created_at"]

class CampaignSerializer(serializers.ModelSerializer):
    creator_username = serializers.CharField(source="creator.username", read_only=True)
    content_type = serializers.ChoiceField(choices=Campaign._meta.get_field("content_type").choices, required=True)
    media_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    class Meta:
        model = Campaign
        fields = ["id","creator","creator_username","platform","content_type","objective","url","profile_url","title","thumbnail_url","media_seconds","description","budget","approved_budget","budget_package","target_quantity","subscription_days","subscription_until","is_free_trial","status","admin_note","created_at"]
        read_only_fields = ["creator","title","thumbnail_url","budget","approved_budget","target_quantity","subscription_days","subscription_until","is_free_trial","status","admin_note","created_at"]
    def validate(self, attrs):
        package = attrs.get("budget_package")
        if self.instance is None and not self.context["request"].user.is_creator:
            raise serializers.ValidationError({"budget_package": "Only creators can select campaign packages."})
        if self.instance is None and package is None:
            raise serializers.ValidationError({"budget_package": "Select an admin-configured campaign budget package."})
        if package and not package.is_active:
            raise serializers.ValidationError({"budget_package": "This campaign budget is no longer available."})
        platform = attrs.get("platform", getattr(self.instance, "platform", None))
        content_type = attrs.get("content_type", getattr(self.instance, "content_type", None))
        objective = attrs.get("objective", getattr(self.instance, "objective", None))
        if content_type not in dict(Campaign.CONTENT_TYPES.get(platform, [])):
            raise serializers.ValidationError({"content_type": "Choose a content type supported by the selected platform."})
        media_seconds = attrs.get("media_seconds", getattr(self.instance, "media_seconds", None))
        if content_type not in Campaign.VIDEO_CONTENT_TYPES and media_seconds is not None:
            raise serializers.ValidationError({"media_seconds": "Only video content types can have a video duration."})
        if package and (package.platform != platform or package.objective != objective):
            raise serializers.ValidationError({"budget_package": "Choose a budget matching the selected platform and objective."})
        if self.instance and package and package.pk != self.instance.budget_package_id:
            raise serializers.ValidationError({"budget_package": "A campaign's budget package cannot be changed after submission."})
        return attrs
    def create(self, validated_data):
        validated_data["creator"] = self.context["request"].user
        package = validated_data.get("budget_package")
        if package:
            validated_data["budget"] = package.amount
            validated_data["target_quantity"] = package.quantity
            validated_data["subscription_days"] = package.validity_days
        return super().create(validated_data)

class PaymentRequestSerializer(serializers.ModelSerializer):
    screenshot = serializers.FileField(write_only=True, required=False)
    evidence_screenshot_url = serializers.SerializerMethodField()
    class Meta:
        model = PaymentRequest
        fields = ["id","request_type","amount","transaction_code","evidence_note","evidence_screenshot_url","screenshot","level","campaign","status","admin_note","admin_payment_details","created_at","reviewed_at"]
        read_only_fields = ["transaction_code","evidence_note","evidence_screenshot_url","status","admin_note","admin_payment_details","created_at","reviewed_at"]
    def get_evidence_screenshot_url(self, obj):
        if not obj.evidence_screenshot:
            return None
        request = self.context.get("request")
        if not request or not request.user.is_staff:
            return None
        return request.build_absolute_uri(f"/api/admin/review/payments/{obj.id}/evidence/")
    def validate_screenshot(self, screenshot):
        if screenshot.size > 8 * 1024 * 1024:
            raise serializers.ValidationError("Screenshot must be 8 MB or smaller.")
        signatures = {
            "image/png": (b"\x89PNG\r\n\x1a\n",),
            "image/jpeg": (b"\xff\xd8\xff",),
            "image/gif": (b"GIF87a", b"GIF89a"),
            "image/webp": (b"RIFF",),
        }
        accepted = signatures.get(screenshot.content_type)
        header = screenshot.read(12)
        screenshot.seek(0)
        if not accepted or not any(header.startswith(signature) for signature in accepted):
            raise serializers.ValidationError("Upload a valid PNG, JPEG, GIF, or WebP screenshot.")
        if screenshot.content_type == "image/webp" and header[8:12] != b"WEBP":
            raise serializers.ValidationError("Upload a valid WebP screenshot.")
        return screenshot
    def validate(self, attrs):
        if self.instance is not None and "request_type" not in attrs:
            return attrs
        user = self.context["request"].user
        request_type = attrs["request_type"]
        level = attrs.get("level")
        campaign = attrs.get("campaign")
        amount = attrs["amount"]
        if amount <= 0:
            raise serializers.ValidationError({"amount": "Payment amount must be greater than zero."})
        if request_type == "CREATOR_ACTIVATION":
            if "CREATOR" not in (user.roles or []):
                raise serializers.ValidationError({"request_type": "A creator role is required for this request."})
            if user.creator_active:
                raise serializers.ValidationError({"request_type": "Your creator account is already active."})
            if level:
                raise serializers.ValidationError({"level": "Creator activation requests do not use a subscription level."})
            if campaign:
                raise serializers.ValidationError({"campaign": "Creator activation requests do not use a campaign."})
            expected_amount = PlatformSettings.current().creator_activation_fee
        elif request_type == "ENGAGER_LEVEL":
            if "ENGAGER" not in (user.roles or []):
                raise serializers.ValidationError({"request_type": "An engager role is required for this request."})
            if user.engager_active or user.selected_level_id:
                raise serializers.ValidationError({"request_type": "An engager level can only be subscribed to once."})
            if level is None:
                raise serializers.ValidationError({"level": "Choose an active engager level."})
            if campaign:
                raise serializers.ValidationError({"campaign": "Engager level requests do not use a campaign."})
            if not level.is_active:
                raise serializers.ValidationError({"level": "This engager level is not active."})
            expected_amount = level.fee
        elif request_type == "CAMPAIGN_BUDGET":
            if "CREATOR" not in (user.roles or []) or not user.creator_active:
                raise serializers.ValidationError({"request_type": "An active creator account is required for a campaign subscription."})
            if level:
                raise serializers.ValidationError({"level": "Campaign subscriptions do not use an engager level."})
            if campaign is None or campaign.creator_id != user.id:
                raise serializers.ValidationError({"campaign": "Choose one of your campaigns."})
            if campaign.status != "APPROVED":
                raise serializers.ValidationError({"campaign": "The campaign must be approved before subscribing."})
            if not campaign.budget_package_id:
                raise serializers.ValidationError({"campaign": "This campaign does not have an admin-configured budget."})
            if campaign.subscription_until and campaign.subscription_until > timezone.now():
                raise serializers.ValidationError({"campaign": "This campaign subscription is already active."})
            expected_amount = campaign.budget
        else:
            raise serializers.ValidationError({"request_type": "This payment request type is not available to users."})
        if amount != expected_amount:
            raise serializers.ValidationError({"amount": f"Amount must match the configured fee of KSh {expected_amount}."})
        pending = PaymentRequest.objects.filter(
            user=user,
            request_type=request_type,
            status__in=("PENDING", "AWAITING_PAYMENT", "UNDER_REVIEW"),
        )
        if request_type == "CAMPAIGN_BUDGET":
            pending = pending.filter(campaign=campaign)
        elif request_type == "CREATOR_ACTIVATION":
            pending = pending.filter(level__isnull=True)
        if pending.exists():
            raise serializers.ValidationError({"request_type": "You already have a pending request of this type."})
        return attrs
    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        validated_data.pop("screenshot", None)
        return super().create(validated_data)

class TaskSerializer(serializers.ModelSerializer):
    campaign_title = serializers.CharField(source="campaign.title", read_only=True)
    campaign_url = serializers.URLField(source="campaign.url", read_only=True)
    platform = serializers.CharField(source="campaign.platform", read_only=True)
    content_type = serializers.CharField(source="campaign.content_type", read_only=True)
    objective = serializers.CharField(source="campaign.objective", read_only=True)
    class Meta:
        model = Task
        fields = ["id","campaign","campaign_title","campaign_url","platform","content_type","objective","prompt","question","seconds"]

class TaskClaimSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskClaim
        fields = ["id","task","started_at","claimed_at","submitted_at","question","answer","status","admin_note"]
        read_only_fields = ["started_at","claimed_at","submitted_at","question","status","admin_note"]

class WithdrawalSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ["id","amount","mpesa_number","status","admin_note","created_at"]
        read_only_fields = ["status","admin_note","created_at"]
    def create(self, validated_data):
        validated_data["user"] = self.context["request"].user
        return super().create(validated_data)
