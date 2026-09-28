# API for HappyFox

Source: https://support.happyfox.com/kb/article/360-api-for-happyfox/ (last updated 2026-07-20 13:37:16 UTC)

HappyFox help-desk provides you with RESTful APIs where you can create a ticket, add updates to a ticket, list tickets and users and much more. JSON, Form Urlencoded and Multipart Form Data are the supported formats for the APIs.

What you need, how you can use it, API version, Resource URI and all other information can be found here on this exclusive page for APIs

- [Generating and Managing API Authentication Keys](https://support.happyfox.com/kb/article/476-create-api-key-auth-code-happyfox/)
- [API Reference: Tickets](https://support.happyfox.com/kb/article/1039-tickets-endpoint/)
- [API Reference: Contacts and Contact Groups](https://support.happyfox.com/kb/article/1092-contacts-and-contact-groups-api-endpoints/)
- [API Reference: Reports [Read Only]](https://support.happyfox.com/kb/article/1088-get-reports-via-api/)
- [Using the API to Manage Multiple-Option Custom Fields](https://support.happyfox.com/kb/article/579-using-api-to-set-multipleoption-custom-fields/)
- [API Reference: Asset Management](https://support.happyfox.com/kb/article/1201-api-for-asset-management/)
- API Reference: Knowledge Base [Read only]
- [API Rate limting](https://support.happyfox.com/kb/article/1148-rate-limiting/)
- [API to create custom fields with dynamic values](https://support.happyfox.com/kb/article/1247-api-to-create-custom-fields-with-dynamic-values/)

> If your HappyFox account is hosted in EU, please use \<account_name>.happyfox.net

> Note: All timestamps in API responses are in UTC

Following are the list of API endpoints to get the details about of system attributes. To set these attributes to tickets, please pass their ID in the respective payload for staff ticket update and staff private note endpoints.

> API key and auth code are to be passed in Basic HTTP authentication format only.

## 1. List categories

API Endpoint: https://\<account_name>.happyfox.com/api/1.1/json/categories/

Method: GET

Example response:

IDS are highlighted in yellow

```json
[
{
"prepopulate_cc": "AR",
"description": "",
"time_spent_mandatory": true,
"public": true,
"id": 3,
"name": "Arturia"
},
{
"prepopulate_cc": "AR",
"description": "",
"time_spent_mandatory": true,
"public": true,
"id": 4,
"name": "Company Employees"
}
]
```

## 2. List staffs/agents

API Endpoint: https://\<account_name>.happyfox.com/api/1.1/json/staff/

Method: GET

Example response:

IDS are highlighted in yellow

```json
[
    {
        "name": "George - Admin",
        "is_account_admin": false,
        "email": "george@happyfox-test.com",
        "role": {
            "name": "Administrator",
            "id": 1
        },
        "active": true,
        "id": 14,
        "categories": [
            3,
            4
        ],
        "permissions": [
            "edit_subject",
            "manage_statuses",
            "manage_ticket_custom",
            "manage_client_custom",
            "manage_integrations",
            "set_due_date",
            "manage_categories",
            "manage_priorities",
            "manage_canned_actions",
            "all_tickets",
            "manage_templates",
            "delete_contacts",
            "delete_unassociated_contacts",
            "manage_security",
            "manage_satisfaction_surveys",
            "send_sms",
            "manage_sms",
            "delete_tags",
            "add_tags",
            "manage_tags",
            "manage_multibranding",
            "edit_staff_note",
            "edit_own_note",
            "manage_support_center_translations",
            "manage_support_center_code_customizations",
            "edit_own_reply",
            "view_asset_reports",
            "edit_staff_reply",
            "edit_contact_reply",
            "manage_staff_password",
            "manage_contact_password",
            "reduce_available_time_for_contact_group",
            "manage_import_assets",
            "edit_contacts",
            "manage_agent_script",
            "launch_agent_script",
            "manage_smart_rules",
            "view_contact_reports",
            "view_contact_group_reports",
            "view_satisfaction_survey_reports",
            "view_distribution_reports",
            "manage_support_center_login_settings",
            "manage_support_center_gdpr_settings",
            "view_time_spent_reports",
            "view_canned_actions_reports",
            "view_sla_reports",
            "view_performance_reports",
            "forward_tickets",
            "export_reports",
            "act_on_closed",
            "reassign_tickets",
            "manage_sla",
            "export_contacts",
            "manage_shared_views",
            "manage_inventory",
            "change_ticket_contact",
            "bulk_edit_more_than_50_tickets",
            "manage_personal_views",
            "view_inflow_reports",
            "manage_ticket_templates",
            "view_agent_activity_reports",
            "unlock_staff",
            "unlock_contact",
            "create_edit_github_issue",
            "view_smartrule_reports",
            "move_tickets",
            "manage_contacts",
            "manage_contact_portal_preferences",
            "create_ticket_in_any_category",
            "manage_support_center_seo",
            "manage_contact_group_custom",
            "manage_billing",
            "create_and_add_tags",
            "show_basic_ticket_info_of_unassociated_categories",
            "manage_languages",
            "add_existing_tags",
            "create_edit_azure_devops_workitem",
            "move_ticket_to_any_category",
            "delete_tickets",
            "delete_replies",
            "close_tickets",
            "split_tickets",
            "assign_tickets",
            "create_contacts",
            "export_audit_logs",
            "view_any_assignment_tickets_in_reports",
            "manage_support_center",
            "add_call_notes",
            "create_edit_jira_issue",
            "delete_attachments",
            "bulk_assign_tickets_in_queue",
            "merge_tickets",
            "email_reply",
            "manage_support_center_appearance",
            "manage_staff",
            "view_custom_field_reports",
            "manage_asset_fields",
            "bulk_edit_50_tickets_or_fewer",
            "manage_custom_domain",
            "manage_task_templates",
            "edit_ticket_asset_info",
            "export_asset_info",
            "add_available_time_for_contact_group",
            "manage_scheduled_tickets",
            "manage_forum",
            "manage_asset_types",
            "manage_data_privacy",
            "view_audit_logs",
            "create_edit_wrike_task",
            "create_edit_asana_task",
            "create_manage_pager_duty_incident",
            "create_edit_gitlab_issue",
            "view_contacts_tab",
            "view_contact_groups_tab",
            "view_unassociated_contact_group",
            "access_all_kb_sections_and_articles",
            "manage_custom_dashboard",
            "view_article_reports",
            "make_outgoing_calls",
            "view_created_tickets_in_all_categories",
            "manage_assets",
            "view_subscribed_tickets_in_all_categories",
            "manage_ticket_tasks",
            "create_edit_reports",
            "view_reports",
            "web_reply",
            "view_all_categories_data_in_reports",
            "manage_service_catalog",
            "manage_service_catalog_items",
            "manage_channels",
            "relate_tickets",
            "trigger_manual_survey",
            "manage_kb",
            "manage_voice",
            "manage_contact_groups",
            "manage_account_admins",
            "edit_unassociated_contacts",
            "view_unassociated_contacts",
            "edit_task_assignee"
        ]
    }
]
```

## 3. List all statuses

API Endpoint: https://\<account_name>.happyfox.com/api/1.1/json/statuses/

Method: GET

Example response:

IDS are highlighted in yellow

```json
[
    {
        "name": "On hold",
        "color": "120D58",
        "order": 1,
        "default": false,
        "behavior": "pending",
        "id": 6
    },
    {
        "name": "Follow up",
        "color": "C409AD",
        "order": 6,
        "default": false,
        "behavior": "pending",
        "id": 9
    },
    {
        "name": "Closed",
        "color": "99CC00",
        "order": 2,
        "default": false,
        "behavior": "completed",
        "id": 4
    },
    {
        "name": "In Progress",
        "color": "0066CC",
        "order": 3,
        "default": false,
        "behavior": "pending",
        "id": 2
    },
    {
        "name": "New",
        "color": "BCEDAD",
        "order": 4,
        "default": false,
        "behavior": "pending",
        "id": 7
    }
]
```

## 4. List metadata of all ticket custom fields

API Endpoint: https://\<account_name>.happyfox.com/api/1.1/json/ticket_custom_fields/

Method: GET

Example response:

IDs of the fields are marked in yellow

IDs of the options of dropdown fields are marked in orange

```json
[
    {
        "name": "Request Survey",
        "depends_on_choice": null,
        "required": false,
        "compulsory_on_completed": true,
        "choices": [
            {
                "text": "No",
                "id": 2,
                "dependant_fields": []
            },
            {
                "text": "Yes",
                "id": 1,
                "dependant_fields": []
            }
        ],
        "compulsory_on_move": false,
        "type": "choice",
        "id": 61,
        "categories": [
            {
                "category": 3,
                "order": 1
            },
            {
                "category": 4,
                "order": 2
            }
        ],
        "visible_to_staff_only": false
    }
]
```

## 5. List metadata of all contact custom fields

API Endpoint: https://\<account_name>.happyfox.com/api/1.1/json/user_custom_fields/

Method: GET

Refer ticket_custom_fields for example response

## 6. Export Knowledge Base

HTTP Method: GET

API Endpoints:

External articles - https://\<account_name>.happyfox.com/api/1.1/json/kb/articles/

Internal articles - https://\<account_name>.happyfox.com/api/1.1/json/kb/internal-articles/

All sections - https://\<account_name>.happyfox.com/api/1.1/json/kb/sections/

Export single article - https://\<account_name>.happyfox.com/api/1.1/json/kb/article/\<id_of_the_article>

Export single section - https://\<account_name>.happyfox.com/api/1.1/json/kb/section/\<id_of_the_section>

The above url allows you to access one article a time, using the article id provided from the /articles/ endpoint in the previous step. This is applicable only for external articles.
