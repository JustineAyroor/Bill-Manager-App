# Bill Manager App Specification

This document captures the current functional scope, technical design, operating assumptions, and likely next steps for the Bill Manager App.

It is intended to help with:

- future development planning
- onboarding new collaborators
- understanding current constraints
- keeping product decisions aligned with the real use case

## 1. Product Summary

Bill Manager App is a lightweight shared-bill tracking system for one owner and a small group of members who participate in recurring plans or shared invoices.

The current primary use case is:

- one admin or owner manages the plan
- members owe portions of recurring invoices
- the owner records invoices, allocations, and payments
- the system calculates balances
- reminders can be sent through email, SMS, or WhatsApp
- members can view their relevant information and manage their own preferences and password

This app is intentionally optimized for a small private user base rather than for a public consumer SaaS model.

## 2. Functional Specification

### 2.1 Roles

The app currently supports two user roles:

#### Owner

The owner can:

- log into the application
- view all members and all financial records
- create and manage members
- create and manage invoices
- assign invoice allocations to members
- record payments
- apply payments to balances
- send reminders
- view reminder logs
- trigger member onboarding and invite email flows

#### Member

The member can:

- log into the application
- view only data relevant to that member
- view invoices where that member has an allocation or involvement
- view balances and reminders relevant to that member
- update communication preferences
- update their password
- view reminder logs

The member should not:

- create or modify global plan data
- send reminders
- edit protected administrative information
- alter other members' data

### 2.2 Authentication and Access Control

Current access control rules:

- only active users can log in
- owner users have `OWNER` role
- member users have `MEMBER` role
- only owners can send reminders
- reminder logs are visible to both owners and members
- members are linked to a `member_id` and should only see data associated with that member

Authentication features currently supported:

- email/password login
- owner account bootstrap script
- member account creation and linking
- password reset code flow
- member password change flow
- browser session persistence through Gradio browser state

### 2.3 Member Management

The member management flow is designed around a practical admin workflow:

- owner creates or updates a member record
- if the member email matches an existing member login, that login is linked
- if the email does not exist, a member login can be created and linked
- invite and onboarding emails can be sent to the member email

Current expectations:

- email should uniquely identify a user login
- owner decides member setup and mapping
- members later maintain only their own preferences and password

### 2.4 Invoice and Allocation Management

The app supports recurring invoice tracking with per-member allocation.

Supported behaviors:

- create invoices by billing period
- assign portions of invoice amounts to members
- store total invoice amount
- compute member obligations from allocations
- limit member invoice visibility to invoices relevant to that member

### 2.5 Payments and Applications

The app distinguishes between:

- raw payment records
- payment applications against balances

Supported behaviors:

- record inbound and outbound payment transactions
- associate payments with members where relevant
- apply payments to member balances
- recalculate balances from allocations and applied amounts

### 2.6 Reminder System

The app supports sending reminders in multiple channels:

- email
- SMS
- WhatsApp

Current operational rules:

- only owners can trigger reminders
- reminder attempts are logged
- delivery metadata should be stored in reminder logs
- member communication preferences should influence reminder behavior

Twilio-specific realities:

- SMS requires a Twilio number configured on the correct account
- WhatsApp sandbox requires recipients to join the sandbox before they can receive sandbox messages
- production WhatsApp messaging requires a proper Twilio WhatsApp setup beyond sandbox mode

### 2.7 Email Workflows

Current email workflows include:

- member invite email
- password reset email
- general reminder email

Email behavior depends on:

- working SMTP credentials
- correct `APP_BASE_URL`

If `APP_BASE_URL` is incorrect, invite and reset emails will contain unusable links.

### 2.8 Seed Import

The app includes an Excel import path to bootstrap historical data.

Current seed flow:

- import allocations from spreadsheet
- import transactions from spreadsheet
- create members as needed
- create invoices as needed
- load historical financial information into the application database

This is intended mainly for initial migration from spreadsheet-based tracking.

## 3. Technical Specification

### 3.1 Application Architecture

The application is a Python monolith with a relatively simple layered structure:

- UI layer in Gradio
- auth and security helpers
- service layer for business logic
- SQLAlchemy ORM models and session handling
- Alembic for schema migrations
- SQLite as the current database

### 3.2 Main Technical Components

#### UI

- Gradio-based interface
- multi-tab experience for dashboard, members, invoices, payments, reminders, applications, and bill import
- role-sensitive UI visibility

#### Backend Logic

- Python service modules under `app/services`
- authentication helpers under `app/auth`
- database models under `app/db`

#### Data Storage

- SQLite database file
- schema managed by SQLAlchemy models and Alembic migrations

#### Integrations

- SMTP for email sending
- Twilio for SMS and WhatsApp
- OpenRouter for LLM-assisted bill import workflows

### 3.3 Important Files

Core application entrypoint:

- [app/main.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/main.py)

Configuration loading:

- [app/core/config.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/core/config.py)

Database models:

- [app/db/models.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/db/models.py)

Authentication service:

- [app/auth/service.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/auth/service.py)

Twilio integration:

- [app/services/twilio_service.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/services/twilio_service.py)

Reminder sending logic:

- [app/services/reminder_sender.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/services/reminder_sender.py)

UI screens:

- [app/ui/screens.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/app/ui/screens.py)

Database bootstrap:

- [create_db.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/create_db.py)

Seed importer:

- [seed/seed_excel.py](/Users/justineayroor/Downloads/projects/tmobile-bill-manager/seed/seed_excel.py)

### 3.4 Environment and Configuration

The app currently depends on environment variables for:

- SMTP configuration
- Twilio configuration
- OpenRouter configuration
- app base URL
- browser session secret

Current deployment model assumes:

- one application instance
- one SQLite database file
- low write concurrency
- trusted small group of users

### 3.5 Deployment Model

Current deployment target:

- Google Cloud VM
- Ubuntu
- Python virtual environment
- app started with `python -m app.main`
- app kept running inside `tmux`
- firewall rule for port `7860`
- static external IP

This is intentionally minimal and cost-conscious.

### 3.6 Performance Expectations

For the current expected scale, the app should be adequate.

Expected usage envelope:

- fewer than 100 users
- low concurrent activity
- light transaction volume
- mostly admin-driven usage

SQLite is acceptable at this scale if usage remains modest.

### 3.7 Current Constraints and Risks

Important current constraints:

- SQLite is not ideal for higher concurrency
- Gradio is acceptable for an internal utility app, but not ideal for long-term complex product UX
- `tmux` is a temporary runtime strategy, not a complete service-management solution
- there is no reverse proxy or HTTPS yet
- there is no production-grade background job system yet
- Twilio sandbox limitations affect WhatsApp testing

## 4. Non-Functional Expectations

### 4.1 Security

Current security expectations:

- passwords are hashed
- inactive users should not authenticate
- members should be scoped to their own data
- owner-only actions should stay owner-only
- secrets should stay in `.env` and never be committed

Still recommended:

- rotate exposed secrets if they were pasted into chat or logs
- use a strong browser session secret in deployment
- eventually add HTTPS
- eventually place the app behind Nginx

### 4.2 Reliability

Current reliability level:

- acceptable for small-scale personal usage
- not yet hardened for unattended production operation

To improve reliability:

- run the app under `systemd`
- add restart-on-failure behavior
- add basic backup strategy for SQLite

### 4.3 Maintainability

The project is reasonably maintainable for a small Python app because:

- the codebase is still compact
- the layers are understandable
- business logic is mostly centralized in services

Maintainability risks to watch:

- large UI logic concentration in one screens file
- potential duplication across reminder and member-management workflows
- growing complexity in stateful Gradio interactions

## 5. Product Assumptions

The app currently assumes:

- one real owner or admin is in charge
- members are known people, not anonymous signups
- onboarding is owner-driven, not self-serve public registration
- this is a private coordination tool, not an open marketplace app
- the user base is small and trusted

These assumptions are important because they justify the current simpler architecture.

## 6. Future Thinking

### 6.1 Near-Term Improvements

These are the most practical next steps:

- add `systemd` service for automatic app startup after VM reboot
- add Nginx as a reverse proxy
- add HTTPS
- improve invite and onboarding polish
- strengthen reminder status tracking and refresh behavior
- improve member-facing UX clarity
- add clearer validation and error states in the UI

### 6.2 Medium-Term Product Improvements

Potential product growth areas:

- better onboarding flow for invited members
- richer reminder scheduling and automation
- reminder templates per channel
- activity history per member
- invoice attachments and bill file archive
- audit trail for admin actions
- better filtering and reporting views
- export tools for balances, payments, and reminder history

### 6.3 Medium-Term Technical Improvements

Potential technical improvements:

- split very large UI screen logic into smaller modules
- move from SQLite to Postgres if concurrency or durability needs increase
- add tests around auth, reminders, and member scoping
- add structured logging
- add backup and restore procedures
- add environment-specific config examples

### 6.4 Long-Term Possibilities

If the app grows beyond the current small-group use case, longer-term options include:

- scheduled automatic reminders through background jobs
- public domain with HTTPS and cleaner onboarding
- containerized deployment
- managed database
- mobile-friendly UI refinement
- per-plan support if the owner wants to manage multiple independent groups
- more advanced access and permission models

## 7. Recommended Operating Posture

For the current expected scale, the best posture is:

- keep the architecture simple
- avoid premature complexity
- prioritize reliability and clarity over feature sprawl
- harden the deployment only as the real usage demands it

That means:

- static IP now
- `tmux` for short-term hosting
- `systemd` next
- Nginx and HTTPS after that
- Postgres only if the app actually outgrows SQLite

## 8. Summary

Bill Manager App is currently a practical private shared-bill management tool with:

- owner and member roles
- allocation and payment tracking
- multi-channel reminders
- member self-service preferences
- invite and reset flows
- Excel migration support
- lightweight low-cost deployment

Its present architecture is appropriate for a small trusted user base and can continue to evolve incrementally without needing a full rebuild.
