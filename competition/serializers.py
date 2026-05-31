from rest_framework import serializers
from .models import CompetitionEntry


class CompetitionEntrySerializer(serializers.ModelSerializer):
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
