This script is designed to retrieve customer data related to GDAP (Granular Delegated Admin Privileges) from Partner Center and Microsoft Graph. The report generated includes information about GDAP relationships, roles assigned, and other relevant details. Let’s break down the key components and steps:

Key Sections and Workflow:

Settings:

Partner Center Client ID: This is the identifier for authenticating with the Partner Center.

Tenant ID for Authentication: Optionally, you can specify your partner tenant ID or use common.

Group Display Names: It specifies group names used for GDAP role assignments, like AdminAgents and HelpdeskAgents.

Output File: The report will be saved to the desktop as Customers_GDAP_Report.csv.

Throttle Setting: Introduces a delay (PerCustomerDelayMs) when processing each customer to avoid throttling issues.

Module Installation:

The script checks if required PowerShell modules (MSAL.PS for Partner Center and Microsoft.Graph for Graph API) are installed. If not, it installs them.

Helper Functions:

Escape-ODataString: This function escapes strings to be safely used in OData queries.

Invoke-RestGetWithRetry: A function to make REST API requests with automatic retries in case of failure.

Invoke-PartnerCenterGetAll: This is used to fetch all customers from the Partner Center API.

Invoke-MgGetAll: Used to fetch data from Microsoft Graph API (to get role definitions, groups, and GDAP relationships).

Authentication:

Partner Center Authentication: Uses MSAL.PS to authenticate and get a token to access Partner Center.

Microsoft Graph Authentication: Uses the Connect-MgGraph cmdlet to authenticate with Microsoft Graph, requesting necessary permissions like DelegatedAdminRelationship.Read.All, RoleManagement.Read.Directory, and Group.Read.All.

Role Definitions Cache:

Role Definitions: Caches role definitions from Microsoft Graph to map role IDs to their display names. These are used to resolve the roles assigned to different groups later.

Group ID Resolution:

Admin Agents and Helpdesk Agents Groups: Resolves the group IDs for AdminAgents and HelpdeskAgents in your partner tenant using Microsoft Graph.

If these groups are not found, the script logs warnings.

Fetching Customer Data:

Customers from Partner Center: The script retrieves all customers from Partner Center.

For each customer, it checks if they have an active GDAP relationship and retrieves the associated roles, start date, end date, and role assignments for AdminAgents and HelpdeskAgents.

Building the Report:

The script constructs a report with the following data for each customer:

Customer Name

Tenant ID

GDAP Enabled (Yes/No)

GDAP Start Date

GDAP End Date

Microsoft Entra Roles (a list of role names)

AdminAgents (Yes/No)

HelpdeskAgents (Yes/No)

Roles Assigned to AdminAgents

Roles Assigned to HelpdeskAgents

Roles are resolved using previously cached role definitions.

Exporting the Data:

After processing all customers, the data is sorted by CustomerName and exported to a CSV file on the desktop.

Key Outputs:

CSV File: The generated report (Customers_GDAP_Report.csv) is saved with the above information.

Logging: During execution, the script prints progress messages, including any errors or warnings related to the retrieval of customer or GDAP data.

Purpose:

The script automates the process of gathering detailed GDAP and role-related data for customers of a partner organization. This is particularly useful for administrators who need to audit or report on GDAP relationships and assigned roles within their organization. The resulting CSV file provides insights into which customers have GDAP enabled, when it started and ended, and which roles have been assigned to specific groups.

How the script works in summary:

Authenticate to Partner Center and Microsoft Graph.

Retrieve customer and GDAP relationship data.

Resolve role assignments and group memberships.

Generate a CSV report with details on GDAP relationships, roles, and group memberships.
