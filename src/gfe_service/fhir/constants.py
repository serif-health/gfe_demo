"""Canonical URLs and code systems from the Da Vinci PCT IG (STU2 / 2.0.x build)."""

PCT = "http://hl7.org/fhir/us/davinci-pct"
SD = f"{PCT}/StructureDefinition"
CS = f"{PCT}/CodeSystem"

# Profiles
P_GFE_INFORMATION_BUNDLE = f"{SD}/davinci-pct-gfe-information-bundle"
P_GFE_BUNDLE = f"{SD}/davinci-pct-gfe-bundle"
P_GFE_MISSING_BUNDLE = f"{SD}/davinci-pct-gfe-missing-bundle"
P_GFE_PACKET = f"{SD}/davinci-pct-gfe-packet"
P_GFE_COMPOSITION = f"{SD}/davinci-pct-gfe-composition"
P_GFE_SUMMARY = f"{SD}/davinci-pct-gfe-summary"
P_GFE_INSTITUTIONAL = f"{SD}/davinci-pct-gfe-institutional"
P_GFE_PROFESSIONAL = f"{SD}/davinci-pct-gfe-professional"
P_GFE_COORDINATION_BUNDLE = f"{SD}/davinci-pct-gfe-coordination-bundle"
P_GFE_COORDINATION_TASK = f"{SD}/davinci-pct-gfe-coordination-task"
P_GFE_CONTRIBUTOR_TASK = f"{SD}/davinci-pct-gfe-contributor-task"
P_ORGANIZATION = f"{SD}/davinci-pct-organization"
P_PRACTITIONER = f"{SD}/davinci-pct-practitioner"
P_COVERAGE = f"{SD}/davinci-pct-coverage"
P_US_CORE_PATIENT = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-patient|7.0.0"

# Extensions
X_REQUEST_INITIATION_TIME = f"{SD}/requestInitiationTime"
X_REQUEST_ORIGINATION_TYPE = f"{SD}/requestOriginationType"
X_GFE_SERVICE_LINKING_INFO = f"{SD}/gfeServiceLinkingInfo"
X_PLANNED_SERVICE_PERIOD = f"{SD}/plannedServicePeriod"
X_PROVIDER_TAXONOMY = f"{SD}/providerTaxonomy"
X_PROVIDER_EVENT_METHODOLOGY = f"{SD}/providerEventMethodology"
X_GFE_DISCLAIMER = f"{SD}/gfeDisclaimer"
X_SERVICE_DESCRIPTION = f"{SD}/serviceDescription"
X_GFE_LINE_CTRL_NUM = f"{SD}/gfeBillingProviderLineItemCtrlNum"
X_DATA_ABSENT_REASON = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"
X_COVERAGE_KIND = "http://hl7.org/fhir/5.0/StructureDefinition/extension-Coverage.kind"
X_TASK_REQUESTED_PERIOD = "http://hl7.org/fhir/5.0/StructureDefinition/extension-Task.requestedPeriod"

# Code systems
CS_DOCUMENT_TYPE = f"{CS}/PCTDocumentTypeTemporaryTrialUse"
CS_DOCUMENT_SECTION = f"{CS}/PCTDocumentSection"
CS_GFE_REQUEST_TYPE = f"{CS}/PCTGFERequestTypeCSTemporaryTrialUse"
CS_ESTIMATE_TYPE_SUMMARY = f"{CS}/PCTEstimateTypeSummaryCSTemporaryTrialUse"
CS_SUPPORTING_INFO_TYPE = f"{CS}/PCTSupportingInfoType"
CS_CLAIM_TYPE = "http://terminology.hl7.org/CodeSystem/claim-type"
CS_V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
CS_PROCESS_PRIORITY = "http://terminology.hl7.org/CodeSystem/processpriority"
CS_PAYEE_TYPE = "http://terminology.hl7.org/CodeSystem/payeetype"
CS_DIAGNOSIS_TYPE = "http://terminology.hl7.org/CodeSystem/diagnosistype"
CS_ORG_TYPE = "http://terminology.hl7.org/CodeSystem/organization-type"
CS_TAXONOMY = "http://nucc.org/provider-taxonomy"
CS_REVENUE = "https://www.nubc.org/CodeSystem/RevenueCodes"
CS_TYPE_OF_BILL = "https://www.nubc.org/CodeSystem/TypeOfBill"
CS_POS = "https://www.cms.gov/Medicare/Coding/place-of-service-codes/Place_of_Service_Code_Set"
CS_LOINC = "http://loinc.org"
NPI_SYSTEM = "http://hl7.org/fhir/sid/us-npi"
TIN_SYSTEM = "urn:oid:2.16.840.1.113883.4.4"

# Code system URI <-> short code type used in the pricing database
CODE_SYSTEM_TO_TYPE = {
    "http://www.ama-assn.org/go/cpt": "CPT",
    "urn:oid:2.16.840.1.113883.6.12": "CPT",
    "http://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets": "HCPCS",
    "https://bluebutton.cms.gov/resources/codesystem/hcpcs": "HCPCS",
    "urn:oid:2.16.840.1.113883.6.285": "HCPCS",
    "http://hl7.org/fhir/sid/ndc": "NDC",
    "http://hl7.org/fhir/sid/icd-10-cm": "ICD10CM",
}
TYPE_TO_CODE_SYSTEM = {
    "CPT": "http://www.ama-assn.org/go/cpt",
    "HCPCS": "http://www.cms.gov/Medicare/Coding/HCPCSReleaseCodeSets",
    "NDC": "http://hl7.org/fhir/sid/ndc",
    "ICD10CM": "http://hl7.org/fhir/sid/icd-10-cm",
}
