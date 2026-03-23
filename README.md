# Datadog Azure Monitor Terraform Configuration

## File Structure
Project contains the Datadog monitors for Azure Operate customers.

This Terraform configuration uses a **numbered file organization system** for clarity and logical execution order.

### File Numbering System

Files are organized by number ranges indicating their purpose and execution order:

```
00-09: Pre-deployment setup (Python scripts, external tools)
01-09: Core Terraform configuration (provider, variables, locals)
10-19: Setup resources (webhooks, prerequisites)
20-29: Monitoring resources (monitors, alerts)
30-39: Visualization resources (dashboards)
40-49: Operations resources (muting, maintenance)
50-59: Post-deployment tools (query drift toggle, utilities)
```

### Pre-Deployment Setup (00-09)
- **`00-initial-setup.py`** - Run FIRST before Terraform
  - Generates `customer-specific.auto.tfvars` from Azure Resource Inventory
  - Sets up Webex room configuration
  - Auto-detects which monitor categories to enable

### Core Configuration Files (01-09) - Required
- **`01-config-provider.tf`** - Terraform and Datadog provider configuration
- **`02-config-variables.tf`** - All variable definitions
- **`03-config-locals.tf`** - Local values for phased deployment webhook routing
- **`terraform.tfvars`** - Baseline configuration (webhooks, thresholds, priorities, muting)
- **`customer-specific.auto.tfvars`** - Auto-generated monitor category toggles

### Setup Resources (10-19)
- **`10-setup-webhooks.tf`** - Automatic Datadog webhook creation
  - Controlled by `enable_create_webhooks` variable

### Monitor Resources (20-29)
- **`20-monitors-baseline.tf`** - 14 baseline infrastructure monitors
- **`21-monitors-customer.tf`** - 32 customer-specific workload monitors

### Dashboard Resources (30-39) - Optional
- **`30-dashboards-baseline.tf`** - 6 dashboards for baseline monitors
- **`31-dashboards-customer.tf`** - 6 dashboards for customer-specific monitors

### Operations Resources (40-49)
- **`40-operations-muting.tf`** - Monitor muting/downtime configuration

### Post-Deployment Tools (50-59)
- **`50-postdeploy-toggle-query-ignore.py`** - Toggle query drift detection
  - Temporarily enable drift detection for structural changes
  - See "Query Drift Management" section below

### Documentation Files
- **`README.md`** - This file (main documentation)
- **`initial_setup_README.md`** - Python setup script documentation
- **`documentation/QUICK_REFERENCE.md`** - Quick start commands and examples
- **`documentation/MONITOR_MUTING_GUIDE.md`** - Complete guide to monitor muting strategies
- **`documentation/BASELINE_MONITORS_SUMMARY.md`** - Summary of all baseline monitors
- **`documentation/DASHBOARDS_GUIDE.md`** - Dashboard configuration and usage

## File Dependencies

```
┌──────────────────────────────────────────────────────┐
│             Core Files (Required)                    │
├──────────────────────────────────────────────────────┤
│ provider.tf                  - Provider config       │
│ variables.tf                 - Variable definitions  │
│ terraform.tfvars             - Baseline settings     │
│ customer-specific.auto.tfvars - Category toggles     │
└──────────────────────────────────────────────────────┘
                    │
        ┌───────────┴───────────┐
        │                       │
        ▼                       ▼
┌───────────────┐       ┌───────────────────┐
│   Baseline    │       │  Customer Specific│
│   Monitors    │       │     Monitors      │
│  (14 total)   │       │    (32 total)     │
└───────────────┘       └───────────────────┘
```

## Prerequisites

Before deploying this Terraform configuration, ensure the following prerequisites are met:

### 1. Datadog Organization

- **Dedicated Datadog org/tenant** for the customer
- Separate organization recommended for isolation and cost tracking
- Obtain from Datadog account management or create via Datadog portal

### 2. Datadog API Credentials

Required for Terraform to manage Datadog resources:

- **Datadog API Key** - Authentication for API requests
- **Datadog APP Key** - Application-specific authentication

**How to obtain:**
1. Log into your Datadog organization
2. Navigate to **Organization Settings → API Keys**
3. Create or copy existing API and APP keys
4. Set as environment variables (see Quick Start section)

### 3. Azure Access & Integration

#### Azure Lighthouse Delegation
- **Lighthouse delegation** configured to customer's Azure tenant
- Provides cross-tenant resource management access
- Required for Datadog to monitor Azure resources

#### Azure App Registration
- **App Registration** created in customer's Azure tenant
- Grants Datadog API access to Azure metrics
- Used for Azure-Datadog integration authentication

#### Azure-Datadog Integration Setup
**CRITICAL:** The Azure-Datadog integration must be configured BEFORE deploying monitors.

Run the Azure integration onboarding script:
- **Script Location:** https://gitlab.com/presidioms/managed-cloud/operate-az/datadog-azure-integration-onboard
- **Purpose:** Configures Azure integration using App Registration credentials
- **Required:** Monitors will fail to collect metrics without this integration

> **Note:** This integration enables Datadog to collect Azure metrics, logs, and resource metadata. Without it, monitors will deploy but cannot function properly.

### 4. Webex Bot Configuration (Optional - for Webex Webhooks)

Required only if using Webex webhook notifications:

#### Create Webex Bot
1. Go to https://developer.webex.com
2. Navigate to **My Webex Apps → Create a Bot**
3. Fill in bot details (name, username, icon)
4. Copy the **Bot Access Token**

#### Store Bot Token Locally
Create a JSON file with your bot token:

**File:** `C:\scripts\BotToken.json` (now handled as GitLab variable)
```json
{
  "bot_token": "your-webex-bot-access-token-here"
}
```

> **Note:** The `03-config-locals.tf` file reads this JSON to configure webhook authentication.

### 5. ServiceNow Authentication (Optional - for ServiceNow Webhooks)

Required only if using ServiceNow webhook notifications:

#### Obtain ServiceNow Credentials
- Base64-encoded Basic authentication token
- Format: `base64(username:password)`
- Obtain from ServiceNow administrator

#### Store Auth Token Locally
Create a JSON file with your ServiceNow auth:

**File:** `C:\scripts\SNC_AUTH.json` (now handled as GitLab variable)
```json
{
  "token": "your-base64-encoded-servicenow-auth-token"
}
```

> **Note:** This token is used by the ServiceNow webhook for ticket creation.

### 6. Azure Resource Inventory (For Initial Setup Script)

Required only if running `00-initial-setup.py`:

- **Azure Resource Inventory XLSX export** from customer's Azure tenant
- Contains list of Azure resources to monitor
- Used by setup script to auto-detect monitor categories
- See `initial_setup_README.md` for detailed requirements

---

### Prerequisites Summary

| Prerequisite | Required For | Optional |
|--------------|--------------|----------|
| Datadog org/tenant | All deployments | No |
| Datadog API & APP keys | All deployments | No |
| Azure Lighthouse delegation | All deployments | No |
| Azure App Registration | All deployments | No |
| Azure-Datadog integration | All deployments | No |
| Webex bot + BotToken.json | Webex webhooks | Yes* |
| ServiceNow auth + SNC_AUTH.json | ServiceNow webhooks | Yes* |
| Azure Resource Inventory XLSX | Initial setup script | Yes** |

\* Optional if not using that webhook type  
\** Optional if manually configuring `customer-specific.auto.tfvars`

---

## Quick Start

### Set Datadog Credentials

**Linux/macOS:**
```bash
export DATADOG_API_KEY="your-datadog-api-key"
export DATADOG_APP_KEY="your-datadog-app-key"
```

**Windows PowerShell:**
```powershell
$env:DATADOG_API_KEY = "your-datadog-api-key"
$env:DATADOG_APP_KEY = "your-datadog-app-key"
```

### Optional: Configure Azure Remote Backend

This configuration includes an optional Azure backend for remote state storage. To use it:

```bash
# Initialize with backend configuration
terraform init \
  -backend-config="resource_group_name=<your-rg>" \
  -backend-config="storage_account_name=<your-sa>" \
  -backend-config="container_name=<your-container>" \
  -backend-config="key=datadog-monitors.tfstate"
```

Or create a `backend.conf` file and use:
```bash
terraform init -backend-config=backend.conf
```

To use local state instead, remove the `backend "azurerm" {}` line from `provider.tf` (aka 01-config-provider.tf).

## Configuration Files

This project uses **two automatically-loaded configuration files**:

### `terraform.tfvars` (Baseline Configuration)
Contains common settings that apply to all monitors:
- Webhook URLs (ServiceNow, Webex)
- Monitoring thresholds (CPU, memory, disk, Redis)
- Priority levels (P2, P3, P4)
- Evaluation delays and renotify intervals
- Dashboard enable/disable flags
- Monitor muting configuration

### `customer-specific.auto.tfvars` (Category Toggles)
Contains enable/disable flags for customer-specific monitor categories:
- App Service monitors (6 monitors)
- Azure Functions monitors (2 monitors)
- SQL Database monitors (5 monitors)
- postgresql monitors (2 monitors)
- Container Apps monitors (4 monitors)
- VM/VMSS monitors (4 monitors)
- networking monitors (4 monitors)
- Backup/Recovery monitors (1 monitor)
- Redis Cache monitors (5 monitors)

**Both files are automatically loaded by Terraform** - no special flags needed!

To enable specific categories, edit `customer-specific.auto.tfvars` and set the desired categories to `true`:
```hcl
enable_appservice_monitors    = true   # Enable App Service monitoring
enable_sqldb_monitors         = true   # Enable SQL DB monitoring
enable_containerapps_monitors = true   # Enable Container Apps monitoring
```

> **📝 Note:** If `customer-specific.auto.tfvars` is not present in the directory, Terraform will use the default values from `variables.tf` (aka 02-config-variables.tf) (all customer-specific categories default to `false`). This means **only baseline monitors will be deployed** without the customer-specific file.

## Deployment Options

### Option 1: Deploy All Monitors (Recommended)
Deploy both baseline and customer-specific monitors:

```bash
# Initialize Terraform (first time only)
terraform init

# Review changes
terraform plan

# Deploy all monitors
terraform apply
```

**Files used:** provider.tf, variables.tf, terraform.tfvars, customer-specific.auto.tfvars, azure_baseline_monitors.tf, azure_customer_specific_monitors.tf

### Option 2: Deploy Only Baseline Monitors
Deploy just the 14 baseline infrastructure monitors:

**Method 1: Remove or rename customer-specific.auto.tfvars**
```bash
# Rename the file (or delete it)
mv customer-specific.auto.tfvars customer-specific.auto.tfvars.disabled

# Deploy - only baseline monitors will be created
terraform init
terraform apply
```

**Method 2: Manually delete customer-specific monitors file**
```bash
# Remove the customer-specific monitors file
rm azure_customer_specific_monitors.tf

# Deploy
terraform init
terraform apply
```

**Files used:** provider.tf, variables.tf, terraform.tfvars, azure_baseline_monitors.tf

> **💡 Tip:** The easiest way to deploy only baseline monitors is to simply not include `customer-specific.auto.tfvars` in your deployment directory. All customer-specific monitors will be disabled by default.

### Option 3: Deploy Only Customer-Specific Monitors
Deploy just the 32 specialized workload monitors:

```bash
# Initialize
terraform init

# Plan only customer monitors (using target syntax)
terraform plan -target="datadog_monitor.Azure_Backup_health_event_found_for_backupinstancenamename"

# Or use a targeted apply approach
# List all customer-specific monitor resources and target them
```

**Files used:** provider.tf, variables.tf, terraform.tfvars, azure_customer_specific_monitors.tf

### Option 4: Selective Deployment by File Exclusion
Use Terraform's file selection to deploy specific combinations:

```bash
# Move unwanted monitor files temporarily
mv azure_customer_specific_monitors.tf ../backup/

# Deploy remaining monitors
terraform apply

# Restore file when needed
mv ../backup/azure_customer_specific_monitors.tf .
```

## File Independence

✅ **Both monitor files are fully independent:**
- Each can be deployed separately
- No cross-references between monitor files
- Both rely only on shared core files (provider, variables)
- Can add new monitor files without modifying existing ones

## Variable Dependencies

All monitors in both files use these variables from `variables.tf` (aka 02-config-variables.tf):

### Webhook Variables
- `webhook_primary` (required - production ServiceNow webhook)
- `webhook_testing` (required - internal testing webhook for phased rollout)
- `webhook_servicenow_presidio` (optional - Presidio-facing notifications)
- `webhook_webex_customer` (optional - customer-facing notifications)

### Configuration Variables
- `enable_create_webhooks` (boolean - enables automatic webhook creation)
- `enable_default_mute` (boolean - enables indefinite mute after deployment)
- `deployment_phase` (1, 2, or 3 - controls webhook routing)
- `evaluation_delay`
- `renotify_interval`
- `managed_by_tag`

### Threshold Variables
- `cpu_threshold_critical` / `cpu_threshold_warning`
- `cpu_threshold_warning_high`
- `memory_threshold_critical` / `memory_threshold_warning`
- `memory_threshold_low` / `memory_threshold_warning_low`
- `disk_threshold_critical` / `disk_threshold_warning`
- `availability_threshold` / `availability_threshold_warning`
- `availability_threshold_vm`
- `sql_cpu_threshold`
- `appservice_cpu_threshold`
- `resource_quota_threshold_warning`
- Redis thresholds (required only if Redis monitors are enabled):
  - `redis_memory_threshold_critical` / `redis_memory_threshold_warning`
  - `redis_latency_threshold_critical`
  - `redis_serverload_threshold_critical` / `redis_serverload_threshold_warning` (defaults provided)

### Priority Variables
- `priority_urgent` (P2)
- `priority_standard` (P3)
- `priority_low` (P4)

**All variables are required for any deployment option.**

---

## Anomaly Detection

Several monitors use **anomaly detection** instead of static thresholds to automatically adapt to your workload patterns:

### Monitors Using Anomaly Detection:
- **Network Interface Byte Rates** (Monitor 9) - Detects unusual network traffic patterns
- **Function Execution Count** (Monitor 13) - Detects unusual function execution patterns
- **Redis Cache Read Count** (Monitor 22) - Detects unusual read operation patterns
- **Redis Cache Write Count** (Monitor 24) - Detects unusual write operation patterns

### Configuration:
- **Algorithm**: `basic` (simple pattern-based detection)
- **Deviations**: `3` standard deviations from normal behavior
- **Direction**: `above` (alerts only on increases)
- **Learning Period**: 12 hours of historical data
- **Alert Window**: 1 hour

These monitors learn normal patterns over time and alert when metrics deviate significantly from baseline behavior, reducing false positives from expected traffic variations.

---

## Monitor Muting (Indefinite Default)

**Monitors are automatically muted indefinitely after deployment** using a Datadog downtime schedule resource. This allows you to validate monitors before manually activating alerts.

### How It Works

- A `datadog_downtime_schedule` resource is created in `40-operations-muting.tf`
- Applies to all monitors with the `managed_by:terraform` tag
- Remains muted indefinitely until manually unmuted
- Controlled by the `enable_default_mute` variable (default: `true`)

### Configuration Options

```hcl
# In terraform.tfvars

# Option 1: Mute all monitors indefinitely (default)
enable_default_mute = true

# Option 2: Deploy all monitors immediately active (no mute)
enable_default_mute = false
```

### Unmuting Monitors

When ready to activate alerts, unmute via Datadog UI:
1. Navigate to **Monitors → Manage Downtimes**
2. Find the downtime (message: "All monitors muted indefinitely")
3. Click **Cancel** to unmute all monitors

---

## Automatic Webhook Creation

This configuration can **automatically create Datadog webhooks** for you during deployment, eliminating manual setup in the Datadog UI.

### What Gets Created

When `enable_create_webhooks` is enabled (default), Terraform will automatically create:
- **Internal Testing Webhook** (`@webhook-Webex_Internal`) - For Phase 1 and Phase 2 notifications
- **Primary Production Webhook** (`@webhook-SNC_Azure_Webhook`) - For Phase 2 and Phase 3 notifications

The webhook resources are defined in `10-setup-webhooks.tf` and use the URLs specified in `terraform.tfvars`.

Webhook variables (`$BOT_TOKEN`, `$ROOM_ID`, `$SNC_AUTH`) are automatically configured via Terraform locals from JSON files:
- `BotToken.json` - Webex bot authentication token
- `WebexRoom.json` - Webex room ID (generated by `00-initial-setup.py`)
- `SNC_AUTH.json` - ServiceNow authentication credentials

### Configuration

```hcl
# In terraform.tfvars

# Automatic webhook creation (default - recommended for new deployments)
enable_create_webhooks = true

# Manual webhook management (use if webhooks already exist in Datadog)
enable_create_webhooks = false
```

### When to Use Each Option

**Enable automatic creation (`true`) when:**
- Deploying to a new Datadog account or organization
- Starting fresh with new webhook integrations
- You want Terraform to fully manage webhook lifecycle

**Disable automatic creation (`false`) when:**
- Webhooks already exist in your Datadog account
- You prefer to manage webhooks manually via Datadog UI
- You're importing existing infrastructure to Terraform

> **Note:** If webhooks already exist with the same names, set this to `false` to avoid conflicts. Alternatively, you can import existing webhooks into Terraform state.

---

## Phased Deployment Strategy

This configuration supports a **3-phase rollout approach** for gradual monitor deployment, allowing you to establish baseline thresholds before full production rollout.

### How It Works

All monitors automatically route notifications based on the `deployment_phase` variable (1, 2, or 3) set in `terraform.tfvars`. Simply change the phase number to transition between deployment stages.

### The 3 Phases

#### Phase 1: Internal Testing (Weeks 1-2)
**Purpose:** Establish baseline thresholds with internal team feedback only

**Configuration:**
```hcl
deployment_phase = 1
```

**What happens:**
- All monitors send alerts to internal Webex channel only
- Team can adjust thresholds based on real-world metrics
- No ServiceNow tickets created during testing phase
- **Webhooks:** `@webhook-Webex_Internal` only

#### Phase 2: Dual Notification (Ongoing)
**Purpose:** Full production alerts while maintaining internal visibility

**Configuration:**
```hcl
deployment_phase = 2
```

**What happens:**
- Monitors send alerts to BOTH Webex (internal) and ServiceNow (production)
- Internal team can monitor alert frequency and accuracy
- Production ServiceNow tickets are created for customer-facing issues
- **Webhooks:** Both `@webhook-Webex_Internal` AND `@webhook-SNC_Azure_Webhook`

#### Phase 3: Production Only (Final State)
**Purpose:** Production-only notifications, internal testing complete

**Configuration:**
```hcl
deployment_phase = 3
```

**What happens:**
- Monitors send alerts only to ServiceNow
- Clean production state with no test webhooks
- Internal team relies on Datadog UI for monitoring
- **Webhooks:** `@webhook-SNC_Azure_Webhook` only

### Transitioning Between Phases

To move from one phase to another:

1. **Edit `terraform.tfvars`** - Change `deployment_phase = X` to your target phase
2. **Review changes** - Run `terraform plan` to see what will be updated
3. **Apply changes** - Run `terraform apply` to update all monitors
4. **Verify** - Check Datadog UI to confirm webhooks are routing correctly

**Example transition from Phase 1 to Phase 2:**

```powershell
# Edit terraform.tfvars
# Change from:
#   deployment_phase = 1
# To:
#   deployment_phase = 2

# Review changes
terraform plan

# Apply if changes look correct
terraform apply

# Verify in Datadog
# - Check that alerts now trigger both webhooks
# - Confirm ServiceNow tickets are being created
```

### Recommended Timeline

- **Phase 1:** 1-2 weeks (adjust thresholds based on baseline metrics)
- **Phase 2:** Ongoing (maintain until confident in alert accuracy)
- **Phase 3:** Permanent production state

### Benefits of Phased Rollout

- **Threshold Tuning:** Establish realistic thresholds before creating production tickets
- **Alert Validation:** Verify monitor accuracy with internal team first
- **Reduced Noise:** Prevent alert fatigue from poorly tuned thresholds
- **Smooth Transition:** Gradual rollout reduces risk of overwhelming support teams
- **Flexibility:** Easy to rollback by changing `terraform.tfvars` values

---

## Query Drift Management

### Overview

By default, all monitors **ignore query drift** to allow operations teams to tune monitor thresholds directly in the Datadog UI without Terraform conflicts. This is managed via `lifecycle` blocks that tell Terraform to ignore changes to monitor queries.

### Current Configuration

All monitors use this lifecycle block:

```hcl
lifecycle {
  ignore_changes = [
    monitor_thresholds,
    query,  # TOGGLE: Comment out to detect structural query changes
  ]
}
```

**What this means:**
- ✅ Operations can freely adjust thresholds in Datadog UI
- ✅ No drift notifications for threshold tuning
- ✅ Terraform won't revert manual threshold changes
- ⚠️ Terraform also won't detect structural query changes

### Toggle Script

Use the **`50-postdeploy-toggle-query-ignore.py`** script to temporarily enable drift detection when you need to make structural query changes via Terraform.

#### Basic Usage

```bash
python 50-postdeploy-toggle-query-ignore.py
```

The script:
- Auto-detects current state (IGNORE or DETECT mode)
- Prompts for confirmation before making changes
- Toggles all monitors with a single command
- Creates backup files (.bak) before modifying
- Runs `terraform fmt` automatically

#### Workflow for Structural Changes

When you need to update monitoring logic (not just thresholds):

```bash
# Step 1: Enable drift detection
python 50-postdeploy-toggle-query-ignore.py
# (Answer "yes" when prompted)

# Step 2: Make your structural query changes
# Edit the .tf files as needed

# Step 3: Apply the changes
terraform plan
terraform apply

# Step 4: Restore drift ignore mode
python 50-postdeploy-toggle-query-ignore.py
# (Answer "yes" when prompted)
```

#### Example Output

```
======================================================================
TOGGLE QUERY DRIFT DETECTION
======================================================================

[*] Current State: IGNORING query drift
    |-- Operations can tune thresholds in Datadog UI
    |-- Terraform ignores threshold changes

[>] Proposed Change: DETECT query drift
    |-- Will COMMENT OUT 'query' in all lifecycle blocks
    |-- Terraform will detect ALL query changes
    |-- Use this to apply structural query updates

[!] After applying changes, remember to toggle back to 'ignore' mode!

======================================================================
Proceed with this change? (yes/no):
```

### When to Use Each Mode

**IGNORE Mode (Default):**
- Day-to-day operations
- Threshold tuning in Datadog UI
- No Terraform drift notifications
- Operations team has full flexibility

**DETECT Mode (Temporary):**
- Making structural query changes via Terraform
- Updating monitoring logic or metric names
- Adding/removing query conditions
- Remember to toggle back after changes!

### Future Enhancement

For advanced drift visibility while maintaining operational flexibility, see `documentation/FUTURE_THRESHOLD_DRIFT_MGMT.md` for a dual-workspace strategy that provides:
- Production workspace (drift ignored)
- Drift detection workspace (periodic visibility)
- Ability to reconcile changes back to Terraform config

---

## Monitor Categories

### Baseline Monitors (azure_baseline_monitors.tf)
Core infrastructure monitoring for standard Azure resources:
- Service Health Events
- Integration Errors
- Resource Quotas
- VM monitoring (CPU, Memory, Disk, Availability)
- SQL Database (CPU, Availability)
- Storage Account Availability
- Key Vault Availability
- Firewall Availability
- Public IP Availability
- Network Connections
- Application Gateway
- Service Request Counts

**Priority Distribution:**
- Urgent (P2): 8 monitors
- Standard (P3): 17 monitors
- Low (P4): 6 monitors

### Customer-Specific Monitors (azure_customer_specific_monitors.tf)
Specialized workload and advanced monitoring:
- Backup Health Events
- SQL Serverless (CPU, Memory)
- SQL Deadlock Detection
- App Service (CPU, Response Time, HTTP Errors, Anomalies)
- Network Interface Byte Rates
- VM Scale Sets
- Function Apps
- Container Apps
- PostgreSQL FlexibleServer
- Application Gateway

**Priority Distribution:**
- Urgent (P2): 5 monitors
- Standard (P3): 12 monitors
- Low (P4): 3 monitors

## Adding New Monitors

### Automated Method (Recommended)

Use the automated monitor ingestion script for standardized monitor additions:

```bash
# 1. Create input.tf IN adding-new-monitors/ folder
cd adding-new-monitors
echo 'resource "datadog_monitor" "..." { ... }' > input.tf

# 2. Run the automated script
python add-new-monitor.py

# 3. Follow interactive prompts
```

The script automatically:
- ✅ Validates monitor configuration
- ✅ Checks for similar existing monitors
- ✅ Classifies as baseline or customer-specific
- ✅ Applies standardizations (webhooks, lifecycle blocks, tags)
- ✅ Inserts into correct file
- ✅ Runs validation tests

**Full Documentation:** [adding-new-monitors/adding-new-monitors-README.md](adding-new-monitors/adding-new-monitors-README.md)

### Manual Method

To add a monitor manually:

1. Choose the appropriate file:
   - Baseline infrastructure → `20-monitors-baseline.tf`
   - Specialized workloads → `21-monitors-customer.tf`

2. Use existing monitors as templates

3. Ensure monitor uses variables from `02-config-variables.tf`

4. Use `${local.phase_webhooks}` for webhook notifications (see Phased Deployment Strategy section)

5. Add lifecycle block for query drift management:
   ```hcl
   lifecycle {
     ignore_changes = [
       monitor_thresholds,
       query, # TOGGLE: Comment out to detect structural query changes
     ]
   }
   ```

**Detailed Manual Workflow:** [adding-new-monitors/WORKFLOW_STEPS.md](adding-new-monitors/WORKFLOW_STEPS.md)

## Removing Monitors

To remove specific monitors:

### Option 1: Delete from file
```bash
# Remove the monitor resource block from the .tf file
# Then apply
terraform apply
```

### Option 2: Use terraform destroy with targeting
```bash
terraform destroy -target="datadog_monitor.monitor_resource_name"
```

### Option 3: Remove entire file
```bash
# Remove or rename the monitor file
mv azure_customer_specific_monitors.tf azure_customer_specific_monitors.tf.disabled

# Apply to remove all monitors from that file
terraform apply
```

## Best Practices

1. **Always include core files:** provider.tf, variables.tf, terraform.tfvars
2. **Test with `terraform plan`** before applying changes
3. **Use version control** to track monitor changes
4. **Document custom monitor additions** in comments
5. **Review webhook configuration** before deploying new monitors
6. **Use consistent naming** for new monitors
7. **Tag monitors appropriately** (monitor_category, categories, etc.)

## Troubleshooting

### "No configuration files" error
- Ensure `provider.tf` (aka 01-config-provider.tf) is in the directory
- Run `terraform init` first

### "Variable not defined" error
- Ensure `variables.tf` (aka 02-config-variables.tf) is present
- Check `terraform.tfvars` has all required values

### Provider configuration error
- Check `provider.tf` (aka 01-config-provider.tf) exists and is valid
- Verify Datadog API/App keys are configured

### Monitors not deploying
- Verify webhook variables are set in `terraform.tfvars`
- Check monitor syntax is valid
- Review `terraform plan` output for errors

## Additional Documentation

- **Phased Deployment Strategy** - See above section for webhook rollout phases
- **Query Drift Management** - See above section for threshold tuning and structural changes
- **Quick Reference** - `documentation/QUICK_REFERENCE.md` for fast commands and examples
- **Monitor Muting** - `documentation/MONITOR_MUTING_GUIDE.md` for muting strategies
- **Future Drift Strategy** - `documentation/FUTURE_THRESHOLD_DRIFT_MGMT.md` for advanced drift detection
- **Variable Customization** - Review `variables.tf` (aka 02-config-variables.tf) and `terraform.tfvars`

# ari-reports-frontend
# git-inference
