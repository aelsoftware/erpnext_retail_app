import frappe
from frappe import _
from frappe.utils import fmt_money
from frappe.utils import flt
from frappe.auth import LoginManager
from werkzeug import Response
import json
from datetime import date, datetime, timedelta
from frappe.utils import nowdate

@frappe.whitelist(allow_guest=True)
def custom_login(email, password):
    login_manager = LoginManager()
    login_manager.authenticate(email, password)
    login_manager.post_login()

    user = frappe.get_doc("User", email)
    api_key = user.api_key
    api_secret = None

    # Check if the user has an API key and secret, generate if not
    if not api_key:
        api_key = frappe.generate_hash(length=15)
        frappe.db.set_value("User", email, "api_key", api_key)

    try:
        api_secret = frappe.utils.password.get_decrypted_password('User', email, fieldname='api_secret')
    except frappe.exceptions.AuthenticationError:
        # Generate a new API secret if it does not exist
        api_secret = frappe.generate_hash(length=15)
        frappe.utils.password.update_password('User', email, api_secret, fieldname='api_secret')

    # Fetch the user's profile picture URL
    profile_picture = user.user_image
    if not profile_picture:
        full_name = user.full_name or user.first_name + " " + user.last_name
        profile_picture = f"https://ui-avatars.com/api/?name={full_name}&color=16794c&background=daf0e1"

    return {
        "key": api_key,
        "secret": api_secret,
        "dp": profile_picture
    }

@frappe.whitelist()
def get_settings():
    settings = frappe.get_single("Retail Settings")
    return settings

@frappe.whitelist()
def update_settings(walk_in_customer, store_name, store_address):
    settings = frappe.get_single("Retail Settings")
    settings.walk_in_customer = walk_in_customer
    settings.store_name = store_name
    settings.store_address = store_address
    settings.save()
    return {"message": _("Settings updated successfully")}

@frappe.whitelist()
def get_customers():
    # Fetch default currency from system settings
    default_currency = frappe.db.get_value('Global Defaults', None, 'default_currency')

    customers = frappe.get_all('Customer', fields=['name', 'customer_name'])
    customer_list = []

    for customer in customers:
        customer_details = frappe.get_doc('Customer', customer.name)
        customer_balance = get_customer_balance(customer.name)
        total_due = get_total_due(customer.name)

        customer_info = {
            "id": customer.name,
            "name": customer_details.customer_name,
            "advance_balance": fmt_money(customer_balance, currency=default_currency),
            "total_due": fmt_money(total_due, currency=default_currency)
        }

        customer_list.append(customer_info)

    return Response(response=json.dumps(customer_list),
            status=200,
            mimetype='application/json')

def get_customer_balance(customer_name):
    """
    Get the customer's advance balance.
    """
    advance_balance = 0.0
    gl_entries = frappe.get_all("GL Entry", filters={"party_type": "Customer", "party": customer_name}, fields=["debit", "credit"])
    for entry in gl_entries:
        advance_balance += entry.credit - entry.debit
    
    if(advance_balance <0):
        advance_balance=0

    return advance_balance

def get_total_due(customer_name):
    """
    Get the customer's total due.
    """
    total_due = 0.0
    gl_entries = frappe.get_all("GL Entry", filters={"party_type": "Customer", "party": customer_name}, fields=["debit", "credit"])
    for entry in gl_entries:
        total_due += entry.debit - entry.credit

    if(total_due <0):
        total_due=0
    
    return total_due

@frappe.whitelist()
def get_item_prices():
    # Fetch Price Lists used for selling
    price_lists = frappe.get_all('Price List', filters={'selling': 1}, fields=['name'])

    #Fetch item prices for each Price List
    item_prices_list = []
    for price_list in price_lists:
        item_prices = frappe.get_all('Item Price', filters={"price_list": price_list.name}, fields=['item_code','name', 'uom', 'price_list_rate'])
        for price in item_prices:
            item_prices_list.append({
                "item_code":price.item_code,
                "name": price.name,
                "uom": price.uom,
                "price": price.price_list_rate
            })

    return Response(response=json.dumps(item_prices_list),
                        status=200,
                        mimetype='application/json')

@frappe.whitelist()
def get_items():
    items = frappe.get_all('Item', fields=['name', 'item_name', 'stock_uom', 'item_code'])
    item_list = []

    for item in items:
        # Fetch item details
        item_details = frappe.get_doc('Item', item.name)

        # Calculate remaining stock
        remaining_stock = frappe.db.sql("""
            SELECT SUM(actual_qty)
            FROM `tabBin`
            WHERE item_code=%s
        """, item.item_code)[0][0] or 0

        # Fetch Price Lists used for selling
        #price_lists = frappe.get_all('Price List', filters={'selling': 1}, fields=['name'])

        # Fetch item prices for each Price List
        # item_prices_list = []
        # for price_list in price_lists:
        #     item_prices = frappe.get_all('Item Price', filters={"item_code": item.item_code, "price_list": price_list.name}, fields=['name', 'uom', 'price_list_rate'])
        #     for price in item_prices:
        #         item_prices_list.append({
        #             "name": price.name,
        #             "uom": price.uom,
        #             "price": price.price_list_rate
        #         })

        # Construct item dictionary
        item_list.append({
            "item_name": item.item_name,
            "item_code": item.item_code,
            "remaining_stock": "{} {}".format(remaining_stock, item_details.stock_uom),
            # "item_prices": item_prices_list
        })

    return Response(response=json.dumps(item_list),
                        status=200,
                        mimetype='application/json')

@frappe.whitelist(allow_guest=True)
def create_sales_invoice():
    try:
        # Get JSON data from request
        data = frappe.form_dict.get('data')
        
        if not data:
            return {"error": "No data provided"}

        invoice_data = frappe.parse_json(data)
        invoice_data['update_stock'] = 1

        # Check if payment_terms_template is provided, if not set a default
        if 'payment_terms_template' not in invoice_data:
            try:
                customer = frappe.get_doc('Customer', invoice_data['customer'])
                if customer.payment_terms:
                    invoice_data['payment_terms_template'] = customer.payment_terms
                else:
                    # Set a default payment terms template if not set for the customer
                    invoice_data['payment_terms_template'] = 'Standard'

                    # Check if 'Standard' payment terms template exists
                    if not frappe.db.exists('Payment Terms Template', 'Standard'):
                        return {
                            "error": "Payment Terms Template 'Standard' does not exist. Please create it."
                        }

            except frappe.DoesNotExistError:
                return {"error": f"Customer {invoice_data['customer']} not found"}

        # Create the sales invoice
        sales_invoice = frappe.get_doc(invoice_data)
        sales_invoice.insert()
        sales_invoice.submit()

        return {"message": "Sales Invoice created successfully", "invoice_name": sales_invoice.name}

    except Exception as e:
        # Use a generic error logging method that doesn't depend on the missing ErrorLog function
        frappe.log_error(frappe.get_traceback(), 'Sales Invoice Creation Error')
        return {"error": str(e)}


@frappe.whitelist(allow_guest=True)
def get_sales_invoices():
    try:
        # Fetch pagination parameters
        page = int(frappe.form_dict.get('page', 1))
        per_page = int(frappe.form_dict.get('per_page', 20))

        # Calculate the start index
        start = (page - 1) * per_page

        # Fetch filter parameters
        customer = frappe.form_dict.get('customer')
        start_date = frappe.form_dict.get('start_date')
        end_date = frappe.form_dict.get('end_date')
        invoice_name = frappe.form_dict.get('invoice_name')

        # Build filters based on parameters
        filters = {}
        if customer:
            filters['customer'] = customer
        if start_date and end_date:
            filters['posting_date'] = ['between', [start_date, end_date]]
        elif start_date:
            filters['posting_date'] = ['>=', start_date]
        elif end_date:
            filters['posting_date'] = ['<=', end_date]
        if invoice_name:
            filters['name'] = ['like', f'%{invoice_name}%']

        # Fetch sales invoices with pagination and filters
        sales_invoices = frappe.get_all('Sales Invoice', 
                                        fields=['name', 'customer', 'due_date', 'posting_date', 'set_posting_time', 'posting_time', 'status', 'grand_total', 'outstanding_amount', 'owner'], 
                                        filters=filters, 
                                        start=start, 
                                        limit=per_page)
        
        invoices = []
        
        for invoice in sales_invoices:
            invoice_doc = frappe.get_doc('Sales Invoice', invoice.name)
            items = []
            for item in invoice_doc.items:
                items.append({
                    "item_code": item.item_code,
                    "item_name": item.item_name,
                    "qty": item.qty,
                    "rate": item.rate,
                    "uom": item.uom
                })

            # Function to handle date serialization
            def serialize_date(obj):
                if isinstance(obj, (datetime, date)):
                    return obj.strftime('%Y-%m-%d')
                elif isinstance(obj, timedelta):
                    return str(obj.total_seconds())
                raise TypeError("Type not serializable")

            invoice_data = {
                "name": invoice.name,
                "doctype": "Sales Invoice",
                "customer": invoice.customer,
                "due_date": serialize_date(invoice.due_date) if invoice.due_date else None,
                "items": items,
                "posting_date": serialize_date(invoice.posting_date) if invoice.posting_date else None,
                "set_posting_time": invoice.set_posting_time,
                "posting_time": str(invoice.posting_time) if invoice.posting_time else None,
                "invoice_total": invoice.grand_total,
                "total_paid": invoice.grand_total - invoice.outstanding_amount,
                "status": invoice.status,
                "created_by": invoice.owner
            }
            invoices.append(invoice_data)
        
        # Pagination metadata
        pagination = {
            "page": page,
            "per_page": per_page,
            "total_count": frappe.db.count('Sales Invoice', filters=filters)
        }

        return Response(response=json.dumps({"invoices": invoices, "pagination": pagination}),
                        status=200,
                        mimetype='application/json')

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'Sales Invoice Fetch Error')
        return Response(response=json.dumps({"status": "failed", "error": str(e)}),
                        status=500,
                        mimetype='application/json')
    
@frappe.whitelist(allow_guest=True)
def get_customers_with_balances():
    try:
        # Pagination parameters
        args = frappe.form_dict
        page_length = int(args.get('page_length', 20))
        page_number = int(args.get('page_number', 1))  # Default to first page if not specified
        start = (page_number - 1) * page_length  # Calculate start index for pagination

        # Get default currency
        default_currency = frappe.db.get_value('Global Defaults', None, 'default_currency')

        # Query to fetch customers with balances
        query = """
            SELECT 
                `tabCustomer`.`customer_name`,
                IFNULL(`tabAddress`.`address_line1`, '') AS address,
                IFNULL(`tabContact`.`mobile_no`, '') AS contact,
                SUM(CASE WHEN `tabSales Invoice`.`outstanding_amount` > 0 THEN `tabSales Invoice`.`outstanding_amount` ELSE 0 END) AS total_debits,
                SUM(CASE WHEN `tabSales Invoice`.`outstanding_amount` < 0 THEN -(`tabSales Invoice`.`outstanding_amount`) ELSE 0 END) AS total_credits
            FROM 
                `tabCustomer`
            LEFT JOIN 
                `tabDynamic Link` ON `tabDynamic Link`.`parent` = `tabCustomer`.`name` AND `tabDynamic Link`.`link_doctype` = 'Address'
            LEFT JOIN 
                `tabAddress` ON `tabAddress`.`name` = `tabDynamic Link`.`parent` AND `tabAddress`.`disabled` = 0
            LEFT JOIN 
                `tabContact` ON `tabContact`.`is_primary_contact` = 1 AND `tabContact`.`name` = `tabCustomer`.`name`
            LEFT JOIN 
                `tabSales Invoice` ON `tabSales Invoice`.`customer` = `tabCustomer`.`name` AND `tabSales Invoice`.`docstatus` = 1
            WHERE 
                (`tabSales Invoice`.`outstanding_amount` > 0 OR `tabSales Invoice`.`outstanding_amount` < 0)
            GROUP BY 
                `tabCustomer`.`name`
            LIMIT {}, {}
        """.format(start, page_length)

        # Fetch results from the database
        results = frappe.db.sql(query, as_dict=True)

        # Replace None with empty string for contact field and format currency
        for result in results:
            if result['contact'] is None:
                result['contact'] = ''

            # Format total_debits and total_credits according to currency settings
            result['total_debits'] = fmt_money(result['total_debits'], currency=default_currency)
            result['total_credits'] = fmt_money(result['total_credits'], currency=default_currency)

        # Determine previous and next page numbers
        previous_page = page_number - 1 if page_number > 1 else None
        next_page = page_number + 1 if len(results) == page_length else None  # If results are less than page_length, no next page

        # Prepare pagination information
        pagination = {
            "page_number": page_number,
            "per_page": page_length,
            "previous_page": previous_page,
            "next_page": next_page,
        }

        # Prepare response
        response = {
            "status": "success",
            "message": "Customers with balances retrieved successfully",
            "customers": results,
            "pagination": pagination
        }

        return Response(response=json.dumps(response),
                        status=200,
                        mimetype='application/json')

    except Exception as e:
        # Log the exception and return a failure response
        frappe.log_error(frappe.get_traceback(), 'Get Customers with Balances Error')
        return Response(response=json.dumps({"status": "failed", "error": str(e)}),
                        status=500,
                        mimetype='application/json')


@frappe.whitelist(allow_guest=True)
def make_customer_payment_entry():
    try:
        import frappe
        import json
        from frappe import _
        from frappe.utils import nowdate, flt

        # Fetching request data
        data = frappe.form_dict.get('data')
        if not data:
            return {"status": "failed", "error": "No data provided"}

        payment_data = frappe.parse_json(data)

        # Validate required fields
        if not payment_data.get('paid_from_account') or not payment_data.get('paid_to_account') or not payment_data.get('amount'):
            return {"status": "failed", "error": "Required fields not provided"}

        # Setting up Payment Entry document
        payment_entry = frappe.new_doc('Payment Entry')
        payment_entry.payment_type = 'Receive'  # Adjust as needed
        payment_entry.posting_date = nowdate()
        payment_entry.paid_from = payment_data['paid_from_account']
        payment_entry.paid_to = payment_data['paid_to_account']
        payment_entry.paid_amount = flt(payment_data['amount'])
        payment_entry.reference_no = payment_data.get('reference_no', '')
        payment_entry.reference_date = payment_data.get('reference_date', nowdate())
        payment_entry.remarks = payment_data.get('remarks', '')

        # Save and submit Payment Entry
        payment_entry.insert()
        payment_entry.submit()

        return {"status": "success", "message": f"Payment Entry {payment_entry.name} created successfully"}

    except Exception as e:
        # Log the exception and return a failure response
        frappe.log_error(frappe.get_traceback(), 'Payment Entry Creation Error')
        return {"status": "failed", "error": str(e)}
