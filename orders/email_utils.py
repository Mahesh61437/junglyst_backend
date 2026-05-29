import logging
from django.conf import settings
from django.template.loader import render_to_string
from core.email import paced_send_mail
from core.models import User

logger = logging.getLogger(__name__)

def send_order_confirmation_emails(order):
    """
    Send order confirmation emails to:
    1. Super admins
    2. Sellers (for their respective sub-orders)
    3. Customer/User
    """
    try:
        # Get super admins
        super_admins = User.objects.filter(is_superuser=True, is_deleted=False)
        
        # Email to customer
        if order.user:
            send_customer_email(order)
        elif order.guest_email:
            send_guest_email(order)
        
        # Email to super admins
        if super_admins.exists():
            admin_emails = [admin.email for admin in super_admins]
            send_admin_email(order, admin_emails)
        
        # Email to sellers
        send_seller_emails(order)
        
        return True
    except Exception as e:
        logger.error("Error sending order confirmation emails: %s", e)
        return False

def send_customer_email(order):
    """Send comprehensive order confirmation to customer"""
    try:
        subject = f"Order Confirmed: {order.order_number}"
        
        # Build order items HTML
        items_html = ""
        for item in order.items.all():
            items_html += f"""
            <tr>
                <td>{item.product_name}</td>
                <td>{item.variant_name}</td>
                <td>{item.quantity}</td>
                <td>₹{item.unit_price}</td>
                <td>₹{float(item.unit_price) * item.quantity}</td>
            </tr>
            """
        
        # Build email body
        message = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <h2>Order Confirmation</h2>
                <p>Thank you for your order! Your order has been successfully placed and payment confirmed.</p>
                
                <h3>Order Details</h3>
                <p><strong>Order Number:</strong> {order.order_number}</p>
                <p><strong>Total Amount:</strong> ₹{order.total_amount}</p>
                <p><strong>Status:</strong> {order.get_status_display()}</p>
                
                <h3>Items Ordered</h3>
                <table border="1" cellpadding="10" style="border-collapse: collapse; width: 100%;">
                    <thead>
                        <tr style="background-color: #f0f0f0;">
                            <th>Product</th>
                            <th>Variant</th>
                            <th>Quantity</th>
                            <th>Unit Price</th>
                            <th>Total</th>
                        </tr>
                    </thead>
                    <tbody>
                        {items_html}
                    </tbody>
                </table>
                
                <h3>Shipping Address</h3>
                <p>
                    {order.shipping_address.get('full_name', 'N/A')}<br>
                    {order.shipping_address.get('address_line1', '')}<br>
                    {order.shipping_address.get('city', '')}, {order.shipping_address.get('state', '')} {order.shipping_address.get('pincode', '')}<br>
                    Phone: {order.shipping_address.get('phone', 'N/A')}<br>
                    Email: {order.shipping_address.get('email', 'N/A')}
                </p>
                
                <h3>Cost Breakdown</h3>
                <p>
                    Subtotal: ₹{order.subtotal}<br>
                    GST: ₹{order.gst_total}<br>
                    <strong>Total: ₹{order.total_amount}</strong>
                </p>
                
                <p>Your order is being prepared by our sellers and will be dispatched soon. You can track your order status on our website.</p>
                
                <p>If you have any questions, please contact us at admin@junglyst.com</p>
                
                <p>Thank you for shopping with Junglyst!</p>
            </body>
        </html>
        """
        
        recipient_email = order.user.email if order.user else order.guest_email
        paced_send_mail(
            subject=subject,
            message=message,
            recipient_list=[recipient_email],
            html_message=message,
        )
    except Exception as e:
        logger.error("Error sending customer email: %s", e)

def send_guest_email(order):
    """Send order confirmation to guest customer"""
    send_customer_email(order)

def send_admin_email(order, admin_emails):
    """Send order notification to super admins"""
    try:
        subject = f"New Order Placed: {order.order_number}"
        
        # Build seller info
        sellers_info = ""
        for sub_order in order.sub_orders.select_related('seller').all():
            seller_name = getattr(getattr(sub_order.seller, 'seller_profile', None), 'store_name', None) or sub_order.seller.username
            item_count = sub_order.items.count()
            sellers_info += f"<li>{seller_name}: {item_count} item(s) - ₹{sub_order.seller_total}</li>"
        
        # Build email message
        message = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <h2>New Order Alert</h2>
                <p>A new order has been placed on Junglyst.</p>
                
                <h3>Order Details</h3>
                <p><strong>Order Number:</strong> {order.order_number}</p>
                <p><strong>Customer:</strong> {order.user.get_full_name() if order.user else 'Guest Customer'}</p>
                <p><strong>Total Amount:</strong> ₹{order.total_amount}</p>
                <p><strong>Number of Sub-Orders:</strong> {order.sub_orders.count()}</p>
                
                <h3>Sellers Involved</h3>
                <ul>
                    {sellers_info}
                </ul>
                
                <h3>Shipping Address</h3>
                <p>
                    {order.shipping_address.get('full_name', 'N/A')}<br>
                    {order.shipping_address.get('address_line1', '')}<br>
                    {order.shipping_address.get('city', '')}, {order.shipping_address.get('state', '')} {order.shipping_address.get('pincode', '')}<br>
                    Phone: {order.shipping_address.get('phone', 'N/A')}
                </p>
                
                <p>Please monitor the order fulfillment and ensure timely delivery.</p>
            </body>
        </html>
        """
        
        paced_send_mail(
            subject=subject,
            message=message,
            recipient_list=admin_emails,
            html_message=message,
        )
    except Exception as e:
        logger.error("Error sending admin email: %s", e)

def send_seller_emails(order):
    """Send order notification to sellers for their respective sub-orders"""
    try:
        for sub_order in order.sub_orders.select_related('seller').all():
            try:
                seller = sub_order.seller
                if not seller.email:
                    continue
                
                subject = f"New Sub-Order Received: {sub_order.sub_order_number}"
                
                # Build order items for this seller
                items_html = ""
                for item in sub_order.items.all():
                    items_html += f"""
                    <tr>
                        <td>{item.product_name}</td>
                        <td>{item.variant_name}</td>
                        <td>{item.quantity}</td>
                        <td>₹{item.unit_price}</td>
                        <td>₹{float(item.unit_price) * item.quantity}</td>
                    </tr>
                    """
                
                seller_name = getattr(getattr(seller, 'seller_profile', None), 'store_name', None) or seller.username
                
                message = f"""
                <html>
                    <body style="font-family: Arial, sans-serif; color: #333;">
                        <h2>New Sub-Order Received</h2>
                        <p>A new order has been placed on your Junglyst store.</p>
                        
                        <h3>Sub-Order Details</h3>
                        <p><strong>Master Order:</strong> {order.order_number}</p>
                        <p><strong>Sub-Order:</strong> {sub_order.sub_order_number}</p>
                        <p><strong>Sub-Order Total:</strong> ₹{sub_order.seller_total}</p>
                        <p><strong>Status:</strong> {sub_order.get_status_display()}</p>
                        
                        <h3>Items in This Sub-Order</h3>
                        <table border="1" cellpadding="10" style="border-collapse: collapse; width: 100%;">
                            <thead>
                                <tr style="background-color: #f0f0f0;">
                                    <th>Product</th>
                                    <th>Variant</th>
                                    <th>Quantity</th>
                                    <th>Unit Price</th>
                                    <th>Total</th>
                                </tr>
                            </thead>
                            <tbody>
                                {items_html}
                            </tbody>
                        </table>
                        
                        <h3>Delivery Address</h3>
                        <p>
                            {order.shipping_address.get('full_name', 'N/A')}<br>
                            {order.shipping_address.get('address_line1', '')}<br>
                            {order.shipping_address.get('city', '')}, {order.shipping_address.get('state', '')} {order.shipping_address.get('pincode', '')}<br>
                            Phone: {order.shipping_address.get('phone', 'N/A')}
                        </p>
                        
                        <h3>Cost Breakdown</h3>
                        <p>
                            Subtotal: ₹{sub_order.subtotal}<br>
                            <strong>Total: ₹{sub_order.seller_total}</strong>
                        </p>
                        
                        <p><strong>Dispatch Deadline:</strong> {sub_order.dispatch_deadline.strftime('%Y-%m-%d %H:%M')} IST</p>
                        <p>Please confirm and prepare the order for shipment within 48 hours.</p>
                        
                        <p>Log in to your seller dashboard to manage this order.</p>
                    </body>
                </html>
                """
                
                paced_send_mail(
                    subject=subject,
                    message=message,
                    recipient_list=[seller.email],
                    html_message=message,
                )
            except Exception as e:
                logger.error("Error sending seller email to %s: %s", sub_order.seller.email, e)
    except Exception as e:
        logger.error("Error in send_seller_emails: %s", e)
