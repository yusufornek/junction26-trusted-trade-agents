"""UBL 2.1 exports. Schema-valid demonstration, not Peppol delivery certification."""
from decimal import Decimal, ROUND_HALF_UP
from xml.etree import ElementTree as ET

from .canonical import decimal_string

CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
ET.register_namespace("cbc", CBC)
ET.register_namespace("cac", CAC)

# All 17 sales units in the challenge catalog, plus measure aliases. UN/ECE
# Recommendation 20 measures, Recommendation 21 packages (X prefix).
UNIT_CODES = {"L": "LTR", "LTR": "LTR", "KG": "KGM", "ST": "H87", "KI": "XCT", "KRT": "XCT",
              "RAS": "XPU", "SK": "XSA", "PAK": "XPK", "BAG": "XBG", "PRK": "XJR", "BOT": "XBO",
              "NIP": "XBE", "CS": "XCS", "PAL": "XPX", "DSP": "XPK", "KAN": "XCA", "DZ": "DZN", "KLE": "XBX"}
RESPONSE_CODES = {"CONFIRMED": "AP", "REJECTED": "RE", "PENDING_APPROVAL": "CA", "CANCELLED": "RE", "EXPIRED": "RE"}


def basic(parent, name, value, **attrs):
    child = ET.SubElement(parent, f"{{{CBC}}}{name}", attrs)
    child.text = str(value)
    return child


def aggregate(parent, name):
    return ET.SubElement(parent, f"{{{CAC}}}{name}")


def party(parent, kind, identifier, name):
    party = aggregate(aggregate(parent, kind), "Party")
    basic(party, "EndpointID", "needs data from Valio Aimo", schemeID="0088")
    basic(aggregate(party, "PartyIdentification"), "ID", identifier)
    basic(aggregate(party, "PartyName"), "Name", name)


def money(value):
    return format(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def export(order, quote, document="order"):
    if document not in {"order", "response"}:
        raise ValueError("document must be order or response")
    response = document == "response"
    kind = "OrderResponse" if response else "Order"
    root = ET.Element(f"{{urn:oasis:names:specification:ubl:schema:xsd:{kind}-2}}{kind}")
    basic(root, "UBLVersionID", "2.1")
    basic(root, "CustomizationID", "urn:fdc:peppol.eu:poacc:trns:" + ("order_response" if response else "order") + ":3")
    basic(root, "ProfileID", "urn:fdc:peppol.eu:poacc:bis:ordering:3")
    basic(root, "ID", order.id + ("-response" if response else ""))
    issued = (order.decided_at if response else order.created_at) or order.created_at
    basic(root, "IssueDate", issued.date().isoformat())
    basic(root, "IssueTime", issued.timetz().isoformat())
    if response:
        basic(root, "OrderResponseCode", RESPONSE_CODES[order.status])
    basic(root, "Note", "gateway-simulated: no supplier order transmitted. Party endpoint identifiers need data from Valio Aimo.")
    for hit in order.policy.hits:
        basic(root, "Note", hit.rule + ": " + hit.detail)
    unknown = sorted({line.sales_unit for line in order.lines if line.sales_unit not in UNIT_CODES})
    for unit in unknown:
        basic(root, "Note", f"Unknown sales unit {unit}: exported as H87; needs data from Valio Aimo.")
    has_tax = all(line.unit_price_with_tax is not None for line in order.lines)
    if not has_tax:
        basic(root, "Note", "Tax amount/rate needs data from Valio Aimo; payable amount shown net.")
    else:
        basic(root, "Note", "Tax amounts derived from upstream gross minus locked net prices; tax categories need data from Valio Aimo.")
    basic(root, "DocumentCurrencyCode", order.currency)
    if response:
        basic(aggregate(root, "OrderReference"), "ID", order.id)
    else:
        basic(aggregate(root, "QuotationDocumentReference"), "ID", quote.id)
    parties = [("BuyerCustomerParty", quote.customer_id, "Customer " + quote.customer_id),
               ("SellerSupplierParty", "gateway-simulated", "Valio Aimo (gateway simulation)")]
    for party_args in (reversed(parties) if response else parties):
        party(root, *party_args)
    period = aggregate(aggregate(root, "Delivery"), "RequestedDeliveryPeriod")
    basic(period, "EndDate", quote.deliver_by.isoformat())
    gross = None
    if has_tax:
        gross = sum(Decimal(money(Decimal(str(line.unit_price_with_tax)) * Decimal(str(line.units_needed))))
                    for line in order.lines)
        basic(aggregate(root, "TaxTotal"), "TaxAmount", money(gross - Decimal(str(order.total))), currencyID=order.currency)
    totals = aggregate(root, "LegalMonetaryTotal" if response else "AnticipatedMonetaryTotal")
    basic(totals, "LineExtensionAmount", money(order.total), currencyID=order.currency)
    basic(totals, "TaxExclusiveAmount", money(order.total), currencyID=order.currency)
    if gross is not None:
        basic(totals, "TaxInclusiveAmount", money(gross), currencyID=order.currency)
    basic(totals, "PayableAmount", money(gross if gross is not None else order.total), currencyID=order.currency)
    for index, line in enumerate(order.lines, 1):
        item_line = aggregate(aggregate(root, "OrderLine"), "LineItem")
        unit_code = UNIT_CODES.get(line.sales_unit, "H87")
        basic(item_line, "ID", index)
        basic(item_line, "Quantity", decimal_string(line.units_needed), unitCode=unit_code)
        basic(item_line, "LineExtensionAmount", money(line.total), currencyID=order.currency)
        price = aggregate(item_line, "Price")
        basic(price, "PriceAmount", decimal_string(line.unit_price), currencyID=order.currency)
        basic(price, "BaseQuantity", "1", unitCode=unit_code)
        item = aggregate(item_line, "Item")
        basic(item, "Name", line.name)
        basic(aggregate(item, "SellersItemIdentification"), "ID", line.product_id)
        if line.gtin:
            basic(aggregate(item, "StandardItemIdentification"), "ID", line.gtin, schemeID="0160")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")
