"""Small synthetic API payloads shaped like the real ones (checked against live responses)."""

from __future__ import annotations

from typing import Any

GREENHOUSE: dict[str, Any] = {
    "jobs": [
        {
            "id": 101,
            "title": "Forward Deployed Engineer (Fort Lauderdale)",
            "location": {"name": "Remote USA"},
            "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/101",
            "first_published": "2026-11-01T10:00:00-05:00",
            "company_name": "Acme",
            "content": "&lt;p&gt;Build AI agents with Python.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;"
            "Pay: $120,000 &amp;ndash; $150,000 per year&lt;/li&gt;&lt;/ul&gt;",
        },
        {
            "id": 102,
            "title": "Account Executive",
            "location": {"name": "New York"},
            "absolute_url": "https://x/102",
            "content": "&lt;p&gt;Sell.&lt;/p&gt;",
        },
    ]
}

LEVER: list[dict[str, Any]] = [
    {
        "id": "abc-1",
        "text": "Solutions Engineer",
        "createdAt": 1793000000000,
        "country": "US",
        "workplaceType": "remote",
        "categories": {"location": "Remote", "allLocations": ["Remote"]},
        "descriptionPlain": "About us.",
        "hostedUrl": "https://jobs.lever.co/acme/abc-1",
        "lists": [{"text": "Requirements", "content": "<li>3+ years</li><li>APIs</li>"}],
        "additionalPlain": "",
        "salaryRange": {
            "min": 90000,
            "max": 120000,
            "currency": "USD",
            "interval": "per-year-salary",
        },
    },
]

ASHBY: dict[str, Any] = {
    "jobs": [
        {
            "id": "u-1",
            "title": "AI Engineer",
            "location": "Miami",
            "isRemote": False,
            "workplaceType": "Hybrid",
            "isListed": True,
            "descriptionPlain": "LLM work.",
            "jobUrl": "https://jobs.ashbyhq.com/acme/u-1",
            "publishedAt": "2026-11-02T12:00:00+00:00",
            "secondaryLocations": [],
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "compensation": {"compensationTierSummary": "$100K – $130K"},
        },
        {
            "id": "u-2",
            "title": "Hidden",
            "location": "Miami",
            "isListed": False,
            "descriptionPlain": "x",
            "jobUrl": "x",
            "compensation": {},
        },
    ]
}

REMOTIVE: dict[str, Any] = {
    "0-legal-notice": "credit us",
    "jobs": [
        {
            "id": 7,
            "url": "https://remotive.com/remote-jobs/7",
            "title": "Integration Engineer",
            "company_name": "Beta",
            "candidate_required_location": "USA",
            "salary": "",
            "publication_date": "2026-11-05T01:00:00",
            "description": "<p>Integrations.</p>",
        },
    ],
}

REMOTEOK: list[dict[str, Any]] = [
    {"last_updated": 1, "legal": "link back to Remote OK"},
    {
        "id": "9",
        "position": "Implementation Specialist",
        "company": "Gamma",
        "location": "",
        "salary_min": 70000,
        "salary_max": 0,
        "url": "https://remoteOK.com/remote-jobs/9",
        "date": "2026-11-06T00:00:00+00:00",
        "description": "<p>Onboard customers.</p>",
    },
]

USAJOBS: dict[str, Any] = {
    "SearchResult": {
        "SearchResultItems": [
            {
                "MatchedObjectDescriptor": {
                    "PositionID": "IT-2026-1",
                    "PositionTitle": "IT Specialist (Systems Integration)",
                    "OrganizationName": "Department of Testing",
                    "PositionLocationDisplay": "Miami, Florida",
                    "PositionURI": "https://www.usajobs.gov/job/1",
                    "PublicationStartDate": "2026-11-01T00:00:00",
                    "ApplicationCloseDate": "2026-11-30T23:59:59",
                    "PositionRemuneration": [
                        {
                            "MinimumRange": "80000",
                            "MaximumRange": "104000",
                            "RateIntervalCode": "PA",
                        }
                    ],
                    "QualificationSummary": "Specialized experience.",
                    "UserArea": {
                        "Details": {
                            "JobSummary": "Integrate systems.",
                            "MajorDuties": ["Duty A"],
                            "RemoteIndicator": False,
                        }
                    },
                }
            }
        ]
    }
}

ADZUNA: dict[str, Any] = {
    "results": [
        {
            "id": "555",
            "title": "<strong>AI Engineer</strong>",
            "company": {"display_name": "Delta"},
            "location": {"display_name": "Boca Raton, Florida"},
            "description": "Build LLM apps...",
            "redirect_url": "https://www.adzuna.com/land/ad/555",
            "created": "2026-11-03T00:00:00Z",
            "salary_min": 110000,
            "salary_max": 140000,
        },
    ]
}
