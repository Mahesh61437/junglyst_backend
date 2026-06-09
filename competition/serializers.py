from rest_framework import serializers
from .models import CompetitionEntry


class CompetitionEntrySerializer(serializers.ModelSerializer):
    """Internal / submitter-side serializer — includes PII. Do NOT use for public list."""
    class Meta:
        model = CompetitionEntry
        fields = ['id', 'name', 'email', 'mobile', 'about_aquarium', 'image_urls', 'instagram_handle', 'follows_instagram', 'submitted_at']
        read_only_fields = ['id', 'image_urls', 'submitted_at']

    def validate_name(self, value):
        if len(value.strip()) < 2:
            raise serializers.ValidationError("Please enter your full name.")
        return value.strip()

    def validate_mobile(self, value):
        digits = ''.join(filter(str.isdigit, value))
        if len(digits) < 10:
            raise serializers.ValidationError("Please enter a valid 10-digit mobile number.")
        return value.strip()

    def validate_about_aquarium(self, value):
        if len(value.strip()) < 30:
            raise serializers.ValidationError("Please write at least 30 characters about your aquascape.")
        return value.strip()


class PublicEntrySerializer(serializers.ModelSerializer):
    """Public-facing — strips email/mobile, exposes vote_count + has_voted."""
    vote_count = serializers.IntegerField(read_only=True)
    has_voted = serializers.SerializerMethodField()
    prize_tier_label = serializers.SerializerMethodField()

    class Meta:
        model = CompetitionEntry
        fields = [
            'id', 'name', 'about_aquarium', 'image_urls', 'instagram_handle',
            'submitted_at', 'vote_count', 'has_voted',
            'prize_tier', 'prize_tier_label', 'is_disqualified',
        ]
        read_only_fields = fields

    def get_has_voted(self, obj):
        voted_ids = self.context.get('voted_entry_ids') or set()
        return obj.id in voted_ids

    def get_prize_tier_label(self, obj):
        return dict(CompetitionEntry.PRIZE_CHOICES).get(obj.prize_tier, '')
