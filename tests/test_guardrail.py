"""One test per blocked pattern (project rule), plus safe text that must pass."""

import pytest

from app.services.guardrail import check_text


def kinds(text: str, terms=()) -> set[str]:
    return {v["type"] for v in check_text(text, terms).violations}


@pytest.mark.parametrize("text", [
    "We will issue a refund today.",
    "You will be refunded within 3 days.",
    "We are processing your refunds.",
    "You will receive compensation for this.",
    "We will compensate you.",
    "We will reimburse the cost.",
    "Here is a voucher for your next order.",
    "Please use this coupon.",
    "We offer a money-back guarantee.",
    "You will get cashback.",
    "Redelivery is free of charge.",
    "We will pay you back.",
    "A credit note will be issued.",
])
def test_blocks_refund_and_compensation(text):
    assert "refund_or_compensation" in kinds(text)


@pytest.mark.parametrize("text", [
    "Call the driver on 9876543210.",
    "Call +91 98765 43210 for help.",
    "Reach us at +91-98765-43210.",
    "Phone (011) 2345-6789-01.",
    "Driver number 98765 43210.",
])
def test_blocks_phone_numbers(text):
    assert "phone_number" in kinds(text)


@pytest.mark.parametrize("text", [
    "This was your fault.",
    "You were not home when we came.",
    "You failed to collect the parcel.",
    "You didn't answer the door.",
    "You did not give the right address.",
    "You gave the wrong address.",
    "It happened because of you.",
    "It is the customer's fault.",
    "The customer was lying.",
    "This is the driver's fault.",
    "The driver was negligent.",
    "The driver stole the parcel.",
    "We blame the courier.",
    "Our driver lied about the delivery.",
    "You are a scammer.",
])
def test_blocks_blame_phrases(text):
    assert "blame_phrase" in kinds(text)


def test_blocks_gps_coordinates():
    assert "gps_coordinates" in kinds("The parcel was scanned at 30.70461, 76.71793.")


def test_blocks_email():
    assert "email_address" in kinds("Write to driver.ravi@example.com")


def test_blocks_police_accusation():
    assert "police_accusation" in kinds("We have reported the police about this.")
    assert "police_accusation" in kinds("An FIR has been filed.")


def test_blocks_private_driver_details():
    terms = ["Ravi Kumar", "9000012345", "DRV-1", "Ravi", "Kumar"]
    assert "private_detail" in kinds("Ravi Kumar delivered it.", terms)
    assert "private_detail" in kinds("Your driver Ravi will call.", terms)
    assert "private_detail" in kinds("Ref DRV-1", terms)


def test_surname_that_is_a_common_word_does_not_block():
    # driver surname "Date": the plain word "date" must not be flagged
    assert check_text("We will confirm a new delivery date.", ["Caleb Date", "Caleb", "Date"]).ok


@pytest.mark.parametrize("text", [
    "Hello Asha, thank you for reaching out about your parcel RX10001. We have reviewed the delivery "
    "record and we are checking the delivery record for your parcel and working to locate it.",
    "We will contact you to arrange a new delivery time that suits you.",
    "We are reviewing the condition of your parcel and will update you on the next steps.",
])
def test_safe_messages_pass(text):
    assert check_text(text).ok
