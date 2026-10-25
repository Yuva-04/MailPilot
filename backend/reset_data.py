
from app.database import engine
from app.models import Campaign, EmailTemplate, Contact
from sqlalchemy.orm import Session

with Session(engine) as db:
    # Delete campaigns before templates they reference.
    db.query(Campaign).delete(synchronize_session=False)
    db.query(EmailTemplate).delete(synchronize_session=False)
    db.query(Contact).delete(synchronize_session=False)

    db.commit()

print("MailPilot data reset completed.")
print("Contacts: 0")
print("Templates: 0")
print("Campaigns: 0")
