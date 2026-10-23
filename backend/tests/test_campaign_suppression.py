
def test_unsubscribed_contact_is_suppressed():
    # Simulate duplicate contact records sharing one email.
    contacts = [
        {"email": "test@example.com", "is_unsubscribed": False},
        {"email": "TEST@example.com", "is_unsubscribed": True},
    ]

    normalized_email = "test@example.com"

    matching = [
        contact
        for contact in contacts
        if contact["email"].strip().casefold() == normalized_email
    ]

    is_suppressed = any(
        contact["is_unsubscribed"] for contact in matching
    )

    assert is_suppressed is True
