"""
Enriches propensity_results.json with additional DB fields:
contact numbers, reachability, late payment history, loan metadata.
Saves to both data/ and public/data/.
"""
import os, sys, json, asyncio, asyncpg
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, '.env'))

INPUT  = os.path.join(BASE_DIR, 'data', 'propensity_results.json')
OUT1   = os.path.join(BASE_DIR, 'data', 'propensity_results.json')
OUT2   = os.path.join(BASE_DIR, 'public', 'data', 'propensity_results.json')

async def enrich():
    with open(INPUT) as f:
        data = json.load(f)

    loan_numbers = [a['loan_number'] for a in data['accounts']]

    conn = await asyncpg.connect(
        host=os.environ['DB_HOST'], port=int(os.environ.get('DB_PORT', 5432)),
        database=os.environ['DB_NAME'], user=os.environ['DB_USER'], password=os.environ['DB_PASS'],
    )

    rows = await conn.fetch('''
        SELECT loan_number,
               primary_contact_number, secondary_contact_number,
               co_applicant_name, co_applicant_contact_number,
               reference_contact_1, reference_contact_2,
               whatsapp_contact_number, email,
               call_reachable, whatsapp_reachable, msg_reachable,
               late_trend_slope, previous_ptp_date, dpd_of_customer,
               follow_up_datetime, disbursal_date, emi_date, tenure,
               principal_outstanding, occupation, address, current_address,
               lender, bounce_amount, risk, payment_status, ptp_amount,
               no_of_late_installment_3m, no_of_late_installment_6m,
               no_of_late_installment_12m, contact_number, alternate_contact_number
        FROM account_details
        WHERE loan_number = ANY($1)
    ''', loan_numbers)

    await conn.close()

    db_map = {str(r['loan_number']): dict(r) for r in rows}
    print(f"Fetched DB data for {len(db_map)} accounts")

    for account in data['accounts']:
        db = db_map.get(account['loan_number'], {})
        account['primary_contact_number']    = db.get('primary_contact_number') or db.get('contact_number')
        account['secondary_contact_number']  = db.get('secondary_contact_number') or db.get('alternate_contact_number')
        account['co_applicant_name']         = db.get('co_applicant_name')
        account['co_applicant_contact']      = db.get('co_applicant_contact_number')
        account['reference_contact_1']       = db.get('reference_contact_1')
        account['reference_contact_2']       = db.get('reference_contact_2')
        account['whatsapp_contact_number']   = db.get('whatsapp_contact_number')
        account['email']                     = db.get('email')
        account['call_reachable']            = db.get('call_reachable', True)
        account['whatsapp_reachable']        = db.get('whatsapp_reachable', False)
        account['msg_reachable']             = db.get('msg_reachable', False)
        account['late_trend_slope']          = db.get('late_trend_slope')
        account['previous_ptp_date']         = str(db['previous_ptp_date']) if db.get('previous_ptp_date') else None
        account['dpd_of_customer']           = db.get('dpd_of_customer')
        account['follow_up_datetime']        = str(db['follow_up_datetime']) if db.get('follow_up_datetime') else None
        account['disbursal_date']            = str(db['disbursal_date']) if db.get('disbursal_date') else None
        account['emi_date']                  = str(db['emi_date']) if db.get('emi_date') else None
        account['tenure']                    = db.get('tenure')
        account['principal_outstanding']     = float(db['principal_outstanding']) if db.get('principal_outstanding') else None
        account['occupation']                = db.get('occupation')
        account['address']                   = db.get('current_address') or db.get('address')
        account['lender']                    = db.get('lender')
        account['bounce_amount']             = float(db['bounce_amount']) if db.get('bounce_amount') else 0
        account['risk']                      = db.get('risk')
        account['payment_status']            = db.get('payment_status')
        account['ptp_amount']                = float(db['ptp_amount']) if db.get('ptp_amount') else None
        account['late_installments_3m']      = db.get('no_of_late_installment_3m', 0)
        account['late_installments_6m']      = db.get('no_of_late_installment_6m', 0)
        account['late_installments_12m']     = db.get('no_of_late_installment_12m', 0)

        if not db:
            print(f"  WARNING: No DB row for {account['loan_number']}")
        else:
            print(f"  Enriched: {account['loan_number']} ({account['name']}) — {account.get('primary_contact_number','no phone')}")

    os.makedirs(os.path.dirname(OUT2), exist_ok=True)
    for path in [OUT1, OUT2]:
        with open(path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        print(f"Saved → {path}")

asyncio.run(enrich())
