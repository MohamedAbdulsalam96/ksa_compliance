# Copyright (c) 2024, Lavaloon and contributors
# For license information, please see license.txt
from __future__ import annotations

import json
import uuid
from typing import cast, Optional

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import SalesInvoice
from erpnext.selling.doctype.customer.customer import Customer
# noinspection PyProtectedMember
from frappe import _
from frappe.core.doctype.file.file import File
from frappe.model.document import Document
from result import is_err

from ksa_compliance import zatca_api as api
from ksa_compliance import zatca_cli as cli
from ksa_compliance.generate_xml import generate_xml_file, generate_einvoice_xml_fielname
from ksa_compliance.invoice import InvoiceMode, InvoiceType
from ksa_compliance.ksa_compliance.doctype.zatca_business_settings.zatca_business_settings import (
    ZATCABusinessSettings)
from ksa_compliance.ksa_compliance.doctype.zatca_integration_log.zatca_integration_log import ZATCAIntegrationLog
from ksa_compliance.output_models.e_invoice_output_model import Einvoice
from ksa_compliance.zatca_api import ReportOrClearInvoiceError, ReportOrClearInvoiceResult
from ksa_compliance import logger


class SalesInvoiceAdditionalFields(Document):
    # begin: auto-generated types
    # This code is auto-generated. Do not modify anything in this block.

    from typing import TYPE_CHECKING

    if TYPE_CHECKING:
        from frappe.types import DF
        from ksa_compliance.ksa_compliance.doctype.additional_seller_ids.additional_seller_ids import \
            AdditionalSellerIDs

        allowance_indicator: DF.Check
        allowance_vat_category_code: DF.Data | None
        amended_from: DF.Link | None
        buyer_additional_number: DF.Data | None
        buyer_additional_street_name: DF.Data | None
        buyer_building_number: DF.Data | None
        buyer_city: DF.Data | None
        buyer_country_code: DF.Data | None
        buyer_district: DF.Data | None
        buyer_postal_code: DF.Data | None
        buyer_provincestate: DF.Data | None
        buyer_street_name: DF.Data | None
        buyer_vat_registration_number: DF.Data | None
        charge_indicator: DF.Check
        charge_vat_category_code: DF.Data | None
        code_for_allowance_reason: DF.Data | None
        integration_status: DF.Literal[
            "", "Ready For Batch", "Resend", "Corrected", "Accepted with warnings", "Accepted", "Rejected",
            "Clearance switched off"]
        invoice_counter: DF.Int
        invoice_hash: DF.Data | None
        invoice_line_allowance_reason: DF.Data | None
        invoice_line_allowance_reason_code: DF.Data | None
        invoice_line_charge_amount: DF.Float
        invoice_line_charge_base_amount: DF.Float
        invoice_line_charge_base_amount_reason: DF.Data | None
        invoice_line_charge_base_amount_reason_code: DF.Data | None
        invoice_line_charge_indicator: DF.Data | None
        invoice_line_charge_percentage: DF.Percent
        invoice_type_code: DF.Data | None
        invoice_type_transaction: DF.Data | None
        other_buyer_ids: DF.Table[AdditionalSellerIDs]
        payment_means_type_code: DF.Data | None
        prepayment_id: DF.Data | None
        prepayment_issue_date: DF.Date | None
        prepayment_issue_time: DF.Data | None
        prepayment_type_code: DF.Data | None
        prepayment_uuid: DF.Data | None
        prepayment_vat_category_tax_amount: DF.Float
        prepayment_vat_category_taxable_amount: DF.Float
        previous_invoice_hash: DF.Data | None
        qr_code: DF.SmallText | None
        reason_for_allowance: DF.Data | None
        reason_for_charge: DF.Data | None
        reason_for_charge_code: DF.Data | None
        sales_invoice: DF.Link
        sum_of_allowances: DF.Float
        sum_of_charges: DF.Float
        supply_end_date: DF.Data | None
        tax_currency: DF.Data | None
        uuid: DF.Data | None
        validation_errors: DF.SmallText | None
        validation_messages: DF.SmallText | None
        vat_exemption_reason_code: DF.Data | None
        vat_exemption_reason_text: DF.SmallText | None

    # end: auto-generated types

    def get_invoice_type(self, settings: ZATCABusinessSettings) -> InvoiceType:
        invoice_type: InvoiceType
        if settings.invoice_mode == InvoiceMode.Standard:
            invoice_type = 'Standard'
        elif settings.invoice_mode == InvoiceMode.Simplified:
            invoice_type = 'Simplified'
        else:
            if self.buyer_vat_registration_number:
                invoice_type = 'Standard'
            else:
                invoice_type = 'Simplified'
        return invoice_type

    def before_insert(self):
        sales_invoice = cast(SalesInvoice, frappe.get_doc('Sales Invoice', self.sales_invoice))
        self.uuid = str(uuid.uuid4())
        self.tax_currency = "SAR"  # Set as "SAR" as a default tax currency value
        self.sum_of_allowances = sales_invoice.total - sales_invoice.net_total
        self.sum_of_charges = self.compute_sum_of_charges(sales_invoice.taxes)
        self.set_buyer_details(sales_invoice)
        self.set_invoice_type_code()
        self.payment_means_type_code = self.get_payment_means_type_code(sales_invoice)

    def after_insert(self):
        settings = ZATCABusinessSettings.for_invoice(self.sales_invoice)
        if not settings:
            return

        self.prepare_for_zatca(settings)

    def on_submit(self):
        settings = ZATCABusinessSettings.for_invoice(self.sales_invoice)
        if not settings:
            return

        self.send_to_zatca(settings)

    def prepare_for_zatca(self, settings: ZATCABusinessSettings):
        invoice_type = self.get_invoice_type(settings)
        counting_settings_id, pre_invoice_counter, pre_invoice_hash = frappe.db.get_values(
            "ZATCA Invoice Counting Settings", {"business_settings_reference": settings.name},
            ["name", "invoice_counter", "previous_invoice_hash"], for_update=True)[0]

        self.invoice_counter = pre_invoice_counter + 1
        self.previous_invoice_hash = pre_invoice_hash

        einvoice = Einvoice(sales_invoice_additional_fields_doc=self, invoice_type=invoice_type)
        # TODO: Revisit this logging
        frappe.log_error("ZATCA Result LOG", message=json.dumps(einvoice.result, indent=2))
        frappe.log_error("ZATCA Error LOG", message=json.dumps(einvoice.error_dic, indent=2))

        invoice_xml = generate_xml_file(einvoice.result, invoice_type)
        result = cli.sign_invoice(settings.lava_zatca_path, invoice_xml, settings.cert_path,
                                  settings.private_key_path)
        validation_result = cli.validate_invoice(settings.lava_zatca_path, result.signed_invoice_path,
                                                 settings.cert_path, self.previous_invoice_hash)

        self.invoice_hash = result.invoice_hash
        self.qr_code = result.qr_code
        self.validation_messages = '\n'.join(validation_result.messages)
        self.validation_errors = '\n'.join(validation_result.errors_and_warnings)
        self.save()

        # To update counting settings data
        logger.info("Start updating invoice counting settings values")

        logger.info(f"Changing invoice counter from: {pre_invoice_counter} -> {self.invoice_counter}")
        frappe.db.set_value("ZATCA Invoice Counting Settings", counting_settings_id, "invoice_counter",
                            self.invoice_counter)

        logger.info(f"Changing invoice hash from: {pre_invoice_hash} -> {self.invoice_hash}")
        frappe.db.set_value("ZATCA Invoice Counting Settings", counting_settings_id, "previous_invoice_hash",
                            self.invoice_hash)

        xml_filename = generate_einvoice_xml_fielname(settings.vat_registration_number,
                                                      einvoice.result['invoice']['issue_date'],
                                                      einvoice.result['invoice']['issue_time'],
                                                      einvoice.result['invoice']['id'])
        file = cast(File, frappe.get_doc(
            {
                "doctype": "File",
                "file_name": xml_filename,
                "attached_to_doctype": "Sales Invoice Additional Fields",
                "attached_to_name": self.name,
                "content": result.signed_invoice_xml,
                "is_private": True,
            }))
        file.insert()

    def send_to_zatca(self, settings: ZATCABusinessSettings) -> None:
        invoice_type = self.get_invoice_type(settings)
        signed_xml = self.get_signed_xml()
        if not signed_xml:
            frappe.throw(_('Could not find signed XML attachment'), title=_('ZATCA Error'))

        self.send_xml_via_api(signed_xml, self.invoice_hash, invoice_type, settings)

    def set_invoice_type_code(self):
        """
        A code of the invoice subtype and invoices transactions.
        The invoice transaction code must exist and respect the following structure:
        - [NNPNESB] where
        - NN (positions 1 and 2) = invoice subtype: - 01 for tax invoice - 02 for simplified tax invoice.
        - P (position 3) = 3rd Party invoice transaction, 0 for false, 1 for true
        - N (position 4) = Nominal invoice transaction, 0 for false, 1 for true
        - E (position 5) = Exports invoice transaction, 0 for false, 1 for true
        - S (position 6) = Summary invoice transaction, 0 for false, 1 for true
        - B (position 7) = Self billed invoice. Self-billing is not allowed (KSA-2, position 7 cannot be ""1"") for
        export invoices (KSA-2, position 5 = 1).
        """
        # Basic Simplified or Tax invoice
        settings = ZATCABusinessSettings.for_invoice(self.sales_invoice)
        self.invoice_type_transaction = "0100000" if self.get_invoice_type(settings) == 'Standard' else '0200000'

        is_debit, is_credit = frappe.db.get_value("Sales Invoice", self.sales_invoice,
                                                  ["is_debit_note", "is_return"])
        if is_debit:
            self.invoice_type_code = "383"
        elif is_credit:
            self.invoice_type_code = "381"
        else:
            self.invoice_type_code = "388"

    def get_payment_means_type_code(self, invoice: SalesInvoice) -> Optional[str]:
        # An invoice can have multiple modes of payment, but we currently only support one. Therefore, we retrieve the
        # first one if any
        if not invoice.payments:
            return None

        mode_of_payment = invoice.payments[0].mode_of_payment
        return frappe.get_value('Mode of Payment', mode_of_payment, 'custom_zatca_payment_means_code')

    def set_buyer_details(self, sales_invoice: SalesInvoice):
        customer_doc = cast(Customer, frappe.get_doc("Customer", sales_invoice.customer))

        self.buyer_vat_registration_number = customer_doc.custom_vat_registration_number

        for item in customer_doc.get("custom_additional_ids"):
            self.append("other_buyer_ids",
                        {"type_name": item.type_name, "type_code": item.type_code, "value": item.value})

    def send_xml_via_api(self, invoice_xml: str, invoice_hash: str, invoice_type: InvoiceType,
                         settings: ZATCABusinessSettings):
        secret = settings.get_password('production_secret')
        if invoice_type == 'Standard':
            result, status_code = api.clear_invoice(server=settings.fatoora_server_url, invoice_xml=invoice_xml,
                                                    invoice_uuid=self.uuid, invoice_hash=invoice_hash,
                                                    security_token=settings.production_security_token,
                                                    secret=secret)
        else:
            result, status_code = api.report_invoice(server=settings.fatoora_server_url, invoice_xml=invoice_xml,
                                                     invoice_uuid=self.uuid, invoice_hash=invoice_hash,
                                                     security_token=settings.production_security_token,
                                                     secret=secret)

        status = ''
        integration_status = get_integration_status(status_code)
        if is_err(result):
            # The IDE gets confused resolving types, so we help it along
            error = cast(ReportOrClearInvoiceError, result.err_value)
            zatca_message = error.response or error.error
        else:
            value = cast(ReportOrClearInvoiceResult, result.ok_value)
            zatca_message = json.dumps(value.to_json(), indent=2)
            status = value.status

        integration_doc = cast(ZATCAIntegrationLog, frappe.get_doc({
            "doctype": "ZATCA Integration Log",
            "invoice_reference": self.sales_invoice,
            "invoice_additional_fields_reference": self.name,
            "zatca_message": zatca_message,
            "zatca_status": status,
        }))
        integration_doc.insert()
        frappe.db.set_value(self.doctype, self.name, "integration_status", integration_status)

    def compute_sum_of_charges(self, taxes: list) -> float:
        total = 0.0
        if taxes:
            for item in taxes:
                total = total + item.tax_amount
        return total

    def get_signed_xml(self) -> str | None:
        attachments = frappe.get_all("File", fields=("name", "file_name", "attached_to_name", "file_url"),
                                     filters={"attached_to_name": self.name,
                                              "attached_to_doctype": "Sales Invoice Additional Fields"})
        if not attachments:
            return None

        name: str | None = None
        for attachment in attachments:
            if attachment.file_name and attachment.file_name.endswith(".xml"):
                name = attachment.name
                break

        if not name:
            return None

        file = cast(File, frappe.get_doc("File", name))
        content = file.get_content()
        if isinstance(content, str):
            return content
        return content.decode('utf-8')


def customer_has_registration(customer_id: str):
    customer_doc = cast(Customer, frappe.get_doc("Customer", customer_id))
    if customer_doc.custom_vat_registration_number in (None, "") and all(
            ide.value in (None, "") for ide in customer_doc.custom_additional_ids):
        return False
    return True


def get_integration_status(code):
    status_map = {
        200: "Accepted",
        202: "Accepted with warning",
        303: "Clearance switched off",
        401: "Rejected",
        400: "Rejected",
        413: "Resend",
        429: "Resend",
        500: "Resend",
        503: "Resend",
        504: "Resend"
    }
    if code and code in status_map:
        return status_map[code]
    else:
        return ''
