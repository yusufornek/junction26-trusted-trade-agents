"""Spec 13: official UBL 2.1 XSD validation and lossless trade-field mapping."""
import asyncio
from decimal import Decimal
from functools import lru_cache
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
import pytest
import xmlschema
from mcp.server.mcpserver.exceptions import ToolError

from app.api import create_app
from app.config import BACKEND
from app.mcp_server import create_server
from app.models import QuoteLineRequest, QuoteRequest
from app.ubl import CAC, CBC, UNIT_CODES, export
from conftest import OPERATOR_HEADERS, agent_headers, rebuild_catalog

NS = {"cac": CAC, "cbc": CBC}


@lru_cache
def schema(kind):
    return xmlschema.XMLSchema(BACKEND / f"vendor/ubl-2.1/xsd/maindoc/UBL-{kind}-2.1.xsd", allow="local")


@pytest.fixture
def trade(gateway, need, upstream, tmp_path, product_rows):
    product_rows[0]["salesUnitGtin"] = "06400000000001"
    product_rows[0]["units"][0]["gtin"] = "06400000000001"
    product_rows[0]["units"].append({"unitId": "ST", "gtin": "06400000000002", "sizeInBaseUnits": 1})
    product_rows[0]["synkkaData"]["names"][0]["value"] += ' <fresh & safe>'
    product_rows[3]["salesUnitGtin"] = "06400000000003"
    rebuild_catalog(gateway, tmp_path, product_rows)
    lookup = gateway.find_offers(need)
    quote = gateway.orders.create_quote(QuoteRequest(customer_id=need.customer_id, deliver_by=need.deliver_by,
        need_ref=lookup.lookup_id, lines=[QuoteLineRequest(product_id="1", quantity_sales_units=2),
                                         QuoteLineRequest(product_id="4", quantity_sales_units=1)]))
    order = gateway.orders.place_order(quote.id, "ubl")
    assert order.status == "CONFIRMED"
    return lookup, quote, order


def test_ac_1_confirmed_order_validates_official_ubl_schema(gateway, trade):
    _, quote, order = trade
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.get(f"/v1/orders/{order.id}/ubl")
        assert result.status_code == 200 and result.headers["content-type"].startswith("application/xml")
        schema("Order").validate(result.text)
        root = ET.fromstring(result.text)
        assert root.find("cbc:ID", NS).text == order.id
        assert root.find("cac:QuotationDocumentReference/cbc:ID", NS).text == quote.id
        assert root.find("cac:Delivery/cac:RequestedDeliveryPeriod/cbc:EndDate", NS).text == str(quote.deliver_by)
        assert root.find("cac:AnticipatedMonetaryTotal/cbc:TaxExclusiveAmount", NS).text == "39.00"
        assert root.find("cac:TaxTotal/cbc:TaxAmount", NS).text == "5.27"
        assert client.get(f"/v1/orders/{order.id}/ubl", headers=agent_headers("other")).status_code == 404
        assert client.get(f"/v1/orders/{order.id}/ubl", headers=OPERATOR_HEADERS).status_code == 200
        assert client.get(f"/v1/orders/{order.id}/ubl", headers={"Authorization": ""}).status_code == 401
        assert client.get(f"/v1/orders/{order.id}/ubl?document=bad").status_code == 422


def test_ac_2_peppol_mapping_and_missing_data_are_explicit(gateway, trade):
    _, _, order = trade
    for file in (BACKEND / "README.md", BACKEND.parent / "docs/specs/13-open-standard-mapping.md"):
        text = file.read_text()
        # Explicit business-field inventory: each must have its own mapping row,
        # with a UBL target or an honest missing-data marker, not a stray mention.
        inventory = {
            "Standard / process identifiers": ("UBLVersionID", "CustomizationID", "ProfileID"),
            "Order.id / response ID": ("cbc:ID", "OrderReference/ID"),
            "Order.created_at / decided_at": ("IssueDate", "IssueTime"),
            "Order.currency": ("DocumentCurrencyCode",),
            "Quote.customer_id, Order.customer_id": ("BuyerCustomerParty",),
            "Seller identity": ("SellerSupplierParty",),
            "Buyer and seller electronic address (mandatory Peppol routing data)": ("EndpointID", "schemeID"),
            "Party legal registration, postal addresses, country codes, VAT identifiers, contacts": ("PartyLegalEntity", "PostalAddress/Country"),
            "Quote.deliver_by": ("Delivery/RequestedDeliveryPeriod/EndDate",),
            "Line index": ("OrderLine/LineItem/ID",),
            "Line.product_id / GTIN": ("Item/SellersItemIdentification/ID",),
            "Line.name": ("Item/Name",),
            "Line.units_needed": ("Quantity", "unitCode"),
            "Line.unit_price": ("PriceAmount",),
            "Line.total / Order.total": ("LineExtensionAmount", "TaxExclusiveAmount"),
            "Tax / gross / payable": ("TaxTotal/TaxAmount", "PayableAmount"),
            "Allowances, charges, rounding, payment means/terms, accounting references, originator references": ("AllowanceCharge", "PaymentMeans", "PaymentTerms"),
            "Order.status": ("OrderResponseCode",),
        }
        rows = {cells[0]: cells for line in text.splitlines() if line.startswith("|")
                if len(cells := [c.strip() for c in line.strip("|").split("|")]) == 3}
        for field, targets in inventory.items():
            assert field in rows, (file, field)
            assert all(target in rows[field][1] for target in targets) or "needs data from Valio Aimo" in rows[field][2], (file, field)
    root = ET.fromstring(gateway.orders.get_order_ubl(order.id))
    endpoints = root.findall(".//cbc:EndpointID", NS)
    assert len(endpoints) == 2
    assert all(e.text == "needs data from Valio Aimo" and e.attrib["schemeID"] == "0088" for e in endpoints)


def test_ac_3_gtins_quantities_prices_and_all_catalog_units(gateway, trade):
    lookup, quote, order = trade
    product = gateway.catalog.get("1")
    assert product.gtin == "06400000000001"
    assert product.unit_gtins == {"KI": "06400000000001", "ST": "06400000000002"}
    assert next(o for o in lookup.offers if o.product_id == "1").gtin == product.gtin
    assert quote.lines[0].gtin == order.lines[0].gtin == product.gtin
    root = ET.fromstring(gateway.orders.get_order_ubl(order.id))
    for line, element in zip(order.lines, root.findall("cac:OrderLine/cac:LineItem", NS), strict=True):
        assert element.find("cac:Item/cac:StandardItemIdentification/cbc:ID", NS).text == line.gtin
        assert Decimal(element.find("cbc:Quantity", NS).text) == Decimal(str(line.units_needed))
        assert element.find("cbc:Quantity", NS).attrib["unitCode"] == UNIT_CODES[line.sales_unit]
        assert Decimal(element.find("cac:Price/cbc:PriceAmount", NS).text) == Decimal(str(line.unit_price))
    assert {"RAS", "SK", "ST", "KI", "PAK", "BAG", "KRT", "KG", "PRK", "BOT", "NIP", "CS", "PAL", "DSP", "KAN", "DZ", "KLE"} <= UNIT_CODES.keys()
    assert (UNIT_CODES["L"], UNIT_CODES["KG"], UNIT_CODES["ST"]) == ("LTR", "KGM", "H87")
    changed = order.model_copy(deep=True)
    changed.lines[0].sales_unit = "UNKNOWN"
    xml = export(changed, quote)
    schema("Order").validate(xml)
    unknown = ET.fromstring(xml)
    assert unknown.find("cac:OrderLine/cac:LineItem/cbc:Quantity", NS).attrib["unitCode"] == "H87"
    assert any("Unknown sales unit UNKNOWN" in (n.text or "") for n in unknown.findall("cbc:Note", NS))


def test_review_ac_2_requires_document_id_mapping(gateway, trade, monkeypatch):
    from pathlib import Path
    original = Path.read_text
    def without_ids(file, *args, **kwargs):
        text = original(file, *args, **kwargs)
        if file.name in {"README.md", "13-open-standard-mapping.md"}:
            return "\n".join(line for line in text.splitlines() if not line.startswith("| Order.id / response ID |"))
        return text
    monkeypatch.setattr(Path, "read_text", without_ids)
    with pytest.raises(AssertionError):
        test_ac_2_peppol_mapping_and_missing_data_are_explicit(gateway, trade)


@pytest.mark.parametrize("status,code", [("CONFIRMED", "AP"), ("REJECTED", "RE"), ("PENDING_APPROVAL", "CA")])
def test_ac_4_decision_response_validates_schema_and_code(gateway, trade, status, code):
    _, quote, order = trade
    if status != "CONFIRMED":
        request = QuoteRequest(customer_id=quote.customer_id, deliver_by=quote.deliver_by, need_ref=quote.need_ref,
                               lines=[QuoteLineRequest(product_id="1", quantity_sales_units=200)])
        new_quote = gateway.orders.create_quote(request)
        order = gateway.orders.place_order(new_quote.id, status)
        if status == "REJECTED":
            order = gateway.orders.reject(order.id, "Ada")
    assert order.status == status
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        result = client.get(f"/v1/orders/{order.id}/ubl?document=response")
    assert result.status_code == 200
    schema("OrderResponse").validate(result.text)
    root = ET.fromstring(result.text)
    assert root.find("cbc:OrderResponseCode", NS).text == code
    assert root.find("cac:OrderReference/cbc:ID", NS).text == order.id
    notes = [n.text for n in root.findall("cbc:Note", NS)]
    for hit in order.policy.hits:
        assert hit.rule + ": " + hit.detail in notes


def test_ac_5_mcp_exports_identical_owned_document(gateway, trade):
    _, _, order = trade
    with TestClient(create_app(gateway), headers=agent_headers("external")) as client:
        expected = {doc: client.get(f"/v1/orders/{order.id}/ubl?document={doc}").text for doc in ("order", "response")}
    async def run():
        owner, other = create_server(gateway, "external"), create_server(gateway, "other")
        tools = await owner.list_tools()
        tool = next(t for t in tools if t.name == "get_order_ubl")
        assert "UBL 2.1" in tool.description
        for doc, xml in expected.items():
            result = await owner.call_tool("get_order_ubl", {"order_id": order.id, "document": doc})
            assert not result.is_error
            assert result.content[0].text == xml
        tool_failure = await other.call_tool("get_order_ubl", {"order_id": order.id})
        assert tool_failure.is_error
        assert tool_failure.structured_content["code"] == "ORDER_NOT_FOUND"
    asyncio.run(run())
