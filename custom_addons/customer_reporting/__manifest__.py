{
    "name": "Customer Reporting",
    "version": "1.0",
    "category": "Sales/CRM",
    "summary": "AI Customer Reporting Module",
    "description": """
        Custom module to handle generated customer reports from Bedrock AI.
    """,
    "depends": ["crm", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/crm_reporting_views.xml",
        "data/cron.xml",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
