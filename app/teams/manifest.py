"""Official Microsoft Teams App Manifest Schema for Felix.

Compatible with Microsoft Teams App Studio and Developer Portal for Teams.
"""

TEAMS_APP_MANIFEST = {
    "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
    "manifestVersion": "1.16",
    "version": "1.0.0",
    "id": "e2f1a94b-70c2-4a8b-96d5-felixbot0001",
    "developer": {
        "name": "Enterprise Payments Engineering",
        "websiteUrl": "https://your-domain.com",
        "privacyUrl": "https://your-domain.com/privacy",
        "termsOfUseUrl": "https://your-domain.com/terms"
    },
    "icons": {
        "color": "color.png",
        "outline": "outline.png"
    },
    "name": {
        "short": "Felix",
        "full": "Felix • Payments Decline Analyst Agent"
    },
    "description": {
        "short": "Enterprise AI Assistant for payment decline diagnostics",
        "full": "Felix investigates payment authorization drops, calculates baseline metrics across networks and issuers, and drafts actionable remediation playbooks directly in your team channels."
    },
    "accentColor": "#1e293b",
    "bots": [
        {
            "botId": "YOUR_MICROSOFT_APP_ID",
            "scopes": [
                "personal",
                "team",
                "groupchat"
            ],
            "supportsFiles": False,
            "isNotificationOnly": False,
            "commandLists": [
                {
                    "scopes": ["team", "personal", "groupchat"],
                    "commands": [
                        {
                            "title": "Investigate declines",
                            "description": "Run diagnostic baseline audit on recent transaction declines"
                        },
                        {
                            "title": "Approval rate breakdown",
                            "description": "Break down authorization rates across networks, banks, or channels"
                        }
                    ]
                }
            ]
        }
    ],
    "permissions": [
        "identity",
        "messageTeamMembers"
    ],
    "validDomains": [
        "your-domain.com"
    ]
}
