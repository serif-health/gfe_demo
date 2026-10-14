"""Synthetic seed data for the demo pricing database.

All names, NPIs (Luhn-valid but fictitious), TINs and dollar amounts are made up. Payer
"Umbrella Insurance Company" / plan "Premim Family Plus" intentionally match the HL7 PCT
IG example resources so the IG's own GFE Information Bundle example can be priced.

The Neuron examples use real, public provider and network entries with **no local prices**:
Emerson Hospital, Brian D. Hoffman, MD, and Cleveland Clinic Foundation.
"""

from __future__ import annotations

# NPIs
BEDFORD = "1520397849"      # Bedford Regional Medical Center (hospital, UB-04 / institutional)
LEXINGTON = "1620483911"    # Lexington Imaging Center (freestanding, CMS-1500 / professional)
CUTTER = "1182940357"       # Dr. Sam Cutter - orthopaedic surgery
EMERSON = "1922103357"      # Emerson Hospital, Concord MA - real NPI, priced live from Serif Health Neuron
HOFFMAN = "1699756924"      # Brian D. Hoffman, MD, TriHealth, Ohio - real NPI, priced live from Neuron
CLEVELAND_CLINIC = "1154448769"   # Cleveland Clinic Foundation hospital, Cleveland OH

BCBSMA_BLUE_CARE_ELECT = "50ec662e-4288-4722-9fd6-0c94041dc6a3"   # Neuron network_template_id
AETNA_OAMC = "e077586e-51ce-4125-8921-c3e3eece1a90"

TIC = "umbrella_in-network-rates_2026-09.json (TiC)"
TIC_ACME = "acme_in-network-rates_2026-09.json (TiC)"
MRF_BEDFORD = "041234567_bedford-regional_standardcharges.csv (hospital MRF)"
CASH_SCHEDULE = "practice self-pay fee schedule 2026"

PAYERS = [
    # payer_id, name, etin, naic, phone, address_line, city, state, postal_code
    ("umbrella", "Umbrella Insurance Company", "ETIN-3200002", "60054", "860-547-5001",
     "680 Asylum Street", "Hartford", "CT", "06155"),
    ("acme", "Acme Health Plan", "ETIN-4400001", "95001", "617-555-0100",
     "1 Federal Street", "Boston", "MA", "02110"),
    ("bcbsma", "Blue Cross Blue Shield of Massachusetts", None, None, None, None, "Boston", "MA", None),
    ("aetna", "Aetna", None, None, None, None, None, None, None),
]

PLANS = [
    # plan_id, payer_id, plan_name, coverage_class_value, plan_type, network_name, price_network_id
    ("umbrella-premim", "umbrella", "Premim Family Plus Plan", "Premim Family Plus", "PPO", "Umbrella Select", None),
    ("acme-hmo-gold", "acme", "Acme HMO Gold", "Acme HMO Gold", "HMO", "Acme Core", None),
    ("bcbsma-blue-care-elect", "bcbsma", "Blue Care Elect PPO", "Blue Care Elect", "PPO", "Blue Care Elect",
     BCBSMA_BLUE_CARE_ELECT),
    ("aetna-oamc", "aetna", "Aetna Open Access Managed Choice", "Aetna OAMC", "PPO", "Open Access Managed Choice",
     AETNA_OAMC),
]

PROVIDERS = [
    # npi, entity_type, name, given, family, tin, taxonomy, taxonomy_display, billing_class, pos,
    # phone, address_line, city, state, postal_code
    (BEDFORD, "organization", "Bedford Regional Medical Center", None, None, "041234567",
     "282N00000X", "General Acute Care Hospital", "institutional", "22",
     "781-555-0100", "200 Springs Road", "Bedford", "MA", "01730"),
    (LEXINGTON, "organization", "Lexington Imaging Center", None, None, "042345678",
     "261QR0200X", "Radiology Clinic/Center", "professional", "11",
     "781-555-0200", "16 Hayden Avenue", "Lexington", "MA", "02421"),
    (CUTTER, "practitioner", "Sam Cutter, MD", "Sam", "Cutter", "044567890",
     "207X00000X", "Orthopaedic Surgery Physician", "professional", "22",
     "781-555-0400", "200 Springs Road", "Bedford", "MA", "01730"),
    (EMERSON, "organization", "Emerson Hospital", None, None, None,
     "282N00000X", "General Acute Care Hospital", "institutional", "22",
     None, "133 Old Road to Nine Acre Corner", "Concord", "MA", "01742"),
    (HOFFMAN, "practitioner", "Brian D. Hoffman, MD", "Brian", "Hoffman", None,
     "207Q00000X", "Family Medicine", "professional", "11",
     "513-398-3445", "7423 S Mason Montgomery Rd, Ste B", "Mason", "OH", "45040"),
    (CLEVELAND_CLINIC, "organization", "Cleveland Clinic Foundation", None, None, None,
     "282N00000X", "General Acute Care Hospital", "institutional", "22",
     "216-986-1314", "9500 Euclid Ave", "Cleveland", "OH", "44195"),
]

ROLE_TAXONOMIES = [
    ("radiologist", "2085R0202X"), ("radiologist", "2085D0003X"),
    ("surgeon", "207X00000X"), ("surgeon", "207XS0117X"),
    ("anesthesiologist", "207L00000X"),
]

CODE_DESCRIPTIONS = [
    ("CPT", "70450", "CT head without contrast"),
    ("CPT", "71250", "CT chest without contrast"),
    ("CPT", "74176", "CT abdomen and pelvis without contrast"),
    ("CPT", "99213", "Established patient office or other outpatient visit"),
    ("CPT", "70551", "Magnetic resonance (eg, proton) imaging, brain (including brain stem); without contrast material"),
    ("CPT", "73722", "Magnetic resonance (eg, proton) imaging, any joint of lower extremity; with contrast material(s)"),
    ("CPT", "29881", "Arthroscopy, knee, surgical; with meniscectomy (medial OR lateral, including any meniscal shaving)"),
    ("CPT", "01400", "Anesthesia for open or surgical arthroscopic procedures on knee joint; not otherwise specified"),
    ("HCPCS", "A9579", "Injection, gadolinium-based magnetic resonance contrast agent, not otherwise specified, per ml"),
    ("HCPCS", "L1820", "Knee orthosis, elastic with condylar pads and joint, with or without patellar control, prefabricated"),
    ("RC", "0274", "Prosthetic/orthotic devices"),
    ("RC", "0360", "Operating room services - general"),
    ("RC", "0350", "CT scan - general"),
    ("RC", "0611", "Magnetic resonance technology (MRT) - brain/brain stem"),
    ("RC", "0614", "Magnetic resonance technology (MRT) - other MRI"),
    ("RC", "0636", "Drugs requiring detailed coding"),
    ("POS", "11", "Office"),
    ("POS", "22", "On Campus-Outpatient Hospital"),
]

SERVICE_COMPONENTS = [
    # primary_code_type, primary_code, seq, role, code_type, code, modifier, revenue_code, quantity, category
    ("CPT", "70450", 1, "facility", "CPT", "70450", "", "0350", 1, "CT imaging"),
    ("CPT", "71250", 1, "facility", "CPT", "71250", "", "0350", 1, "CT imaging"),
    ("CPT", "74176", 1, "facility", "CPT", "74176", "", "0350", 1, "CT imaging"),
    ("CPT", "73722", 1, "facility", "CPT", "73722", "", "0614", 1, "Imaging"),
    ("CPT", "73722", 2, "facility", "HCPCS", "A9579", "", "0636", 15, "Imaging contrast agent"),
    ("CPT", "73722", 3, "radiologist", "CPT", "73722", "26", None, 1, "Imaging - professional interpretation"),
    ("CPT", "70551", 1, "facility", "CPT", "70551", "", "0611", 1, "Imaging"),
    ("CPT", "70551", 2, "radiologist", "CPT", "70551", "26", None, 1, "Imaging - professional interpretation"),
    ("CPT", "29881", 1, "facility", "CPT", "29881", "", "0360", 1, "Outpatient surgery - facility"),
    ("CPT", "29881", 2, "surgeon", "CPT", "29881", "", None, 1, "Surgery - professional"),
    ("CPT", "29881", 3, "anesthesiologist", "CPT", "01400", "AA", None, 1, "Anesthesia - professional"),
    ("HCPCS", "L1820", 1, "facility", "HCPCS", "L1820", "", "0274", 1, "Durable medical equipment"),
]

_EFF, _EXP = "2026-01-01", "2026-12-31"

NEGOTIATED_RATES = [
    # payer_id, plan_id, npi, tin, billing_class, code_type, code, modifier, type, rate, eff, exp, source
    # --- Bedford Regional (institutional) ---
    ("umbrella", "umbrella-premim", BEDFORD, "041234567", "institutional", "CPT", "73722", "", "negotiated", 1320.00, _EFF, _EXP, TIC),
    ("umbrella", None, BEDFORD, "041234567", "institutional", "CPT", "73722", "", "negotiated", 1450.00, _EFF, _EXP, TIC),
    ("umbrella", None, BEDFORD, "041234567", "institutional", "HCPCS", "A9579", "", "negotiated", 6.50, _EFF, _EXP, TIC),
    ("umbrella", None, BEDFORD, "041234567", "institutional", "HCPCS", "L1820", "", "fee schedule", 310.00, _EFF, _EXP, TIC),
    ("umbrella", "umbrella-premim", BEDFORD, "041234567", "institutional", "CPT", "70551", "", "negotiated", 1090.00, _EFF, _EXP, TIC),
    ("umbrella", None, BEDFORD, "041234567", "institutional", "CPT", "70551", "", "negotiated", 1180.00, _EFF, _EXP, TIC),
    ("acme", None, BEDFORD, "041234567", "institutional", "CPT", "29881", "", "percentage", 45.0, _EFF, _EXP, TIC_ACME),
    # --- Dr. Cutter (surgeon) ---
    ("acme", None, CUTTER, "044567890", "professional", "CPT", "29881", "", "fee schedule", 910.00, _EFF, _EXP, TIC_ACME),
]

STANDARD_CHARGES = [
    # npi, code_type, code, modifier, gross_charge, discounted_cash, source
    (BEDFORD, "CPT", "73722", "", 3200.00, 1600.00, MRF_BEDFORD),
    (BEDFORD, "HCPCS", "A9579", "", 28.00, 12.00, MRF_BEDFORD),
    (BEDFORD, "HCPCS", "L1820", "", 650.00, 380.00, MRF_BEDFORD),
    (BEDFORD, "CPT", "70551", "", 2900.00, 1350.00, MRF_BEDFORD),
    (BEDFORD, "CPT", "29881", "", 14800.00, 6900.00, MRF_BEDFORD),
    (LEXINGTON, "CPT", "70551", "TC", 1100.00, 425.00, CASH_SCHEDULE),
    (CUTTER, "CPT", "29881", "", 2600.00, 1400.00, CASH_SCHEDULE),
]
