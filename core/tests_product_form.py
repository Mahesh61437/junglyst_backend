from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import Product, ProductVariant

User = get_user_model()


def variant_payload(**overrides):
    payload = {
        'name': '', 'variant_type': 'Rhizome', 'base_price': '450', 'stock': '5',
        'item_category': 'light', 'packed_weight_grams': '300',
        'length': '10', 'width': '10', 'height': '10',
    }
    payload.update(overrides)
    return payload


class SellerProductFormValidationTests(APITestCase):
    """Required/optional fields of the seller 'list a specimen' form."""

    def setUp(self):
        self.create_url = reverse('grower_product_create')
        self.seller = User.objects.create_user(
            email='form_seller@example.com', username='form_seller', password='Password123', role='grower'
        )
        self.client.force_authenticate(self.seller)

    def product_payload(self, **overrides):
        payload = {
            'name': 'Bucephalandra Kedagang',
            'description': 'Healthy rhizome, fully acclimated to submerged growth.',
            'variants': [variant_payload()],
            'images': [{'image_url': 'https://cdn.example.com/buce.jpg', 'is_primary': True}],
        }
        payload.update(overrides)
        return payload

    def test_blank_variant_label_is_optional_and_defaults_to_variant_type(self):
        response = self.client.post(self.create_url, self.product_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        variant = ProductVariant.objects.get(product__name='Bucephalandra Kedagang')
        self.assertEqual(variant.name, 'Rhizome')

    def test_typed_variant_label_is_kept(self):
        payload = self.product_payload(variants=[variant_payload(name='XL Clump')])
        response = self.client.post(self.create_url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(ProductVariant.objects.get().name, 'XL Clump')

    def test_draft_can_be_saved_without_description(self):
        payload = {'name': 'Unfinished Anubias', 'description': '', 'is_draft': True, 'is_active': False}
        response = self.client.post(self.create_url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(Product.objects.get(name='Unfinished Anubias').is_draft)

    def test_published_listing_requires_description(self):
        for description in ('', '   '):
            response = self.client.post(self.create_url, self.product_payload(description=description), format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn('description', response.data)

    def test_published_listing_missing_description_key_is_rejected(self):
        payload = self.product_payload()
        del payload['description']
        response = self.client.post(self.create_url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('description', response.data)

    def test_variant_errors_are_returned_per_field(self):
        payload = self.product_payload(variants=[variant_payload(packed_weight_grams='0', length='-1')])
        response = self.client.post(self.create_url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('packed_weight_grams', response.data['variants'][0])

    def test_non_grower_cannot_list_products(self):
        buyer = User.objects.create_user(
            email='form_buyer@example.com', username='form_buyer', password='Password123', role='customer'
        )
        self.client.force_authenticate(buyer)
        response = self.client.post(self.create_url, self.product_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Product.objects.exists())

    def test_other_seller_cannot_edit_or_archive_product(self):
        product = Product.objects.create(name='Owned Moss', description='Lush java moss portion.', seller=self.seller)
        intruder = User.objects.create_user(
            email='other_seller@example.com', username='other_seller', password='Password123', role='grower'
        )
        self.client.force_authenticate(intruder)
        url = reverse('product_detail_id', kwargs={'id': product.id})

        self.assertEqual(self.client.patch(url, {'name': 'Hijacked'}, format='json').status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_403_FORBIDDEN)
        product.refresh_from_db()
        self.assertEqual(product.name, 'Owned Moss')
        self.assertTrue(product.is_active)

    def test_publishing_a_draft_without_description_is_rejected(self):
        draft = Product.objects.create(name='Draft Fern', description='', seller=self.seller, is_draft=True, is_active=False)
        url = reverse('product_detail_id', kwargs={'id': draft.id})

        response = self.client.patch(url, {'is_draft': False, 'is_active': True}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('description', response.data)
        draft.refresh_from_db()
        self.assertTrue(draft.is_draft)

        response = self.client.patch(
            url, {'is_draft': False, 'is_active': True, 'description': 'Lush, ready-to-ship fern.'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_bulk_publish_rejects_drafts_without_description(self):
        draft = Product.objects.create(name='Bare Draft', description='', seller=self.seller, is_draft=True, is_active=False)
        url = reverse('product_bulk_action')

        response = self.client.post(url, {'action': 'publish', 'ids': [str(draft.id)]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Bare Draft', response.data['error'])
        draft.refresh_from_db()
        self.assertTrue(draft.is_draft)

        draft.description = 'Now written up properly.'
        draft.save()
        response = self.client.post(url, {'action': 'publish', 'ids': [str(draft.id)]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_archiving_via_patch_does_not_require_description(self):
        product = Product.objects.create(name='Live Moss', description='Lush java moss portion.', seller=self.seller)
        url = reverse('product_detail_id', kwargs={'id': product.id})

        response = self.client.patch(url, {'is_active': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
