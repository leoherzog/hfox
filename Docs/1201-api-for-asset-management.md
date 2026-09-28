# API for Asset Management

Source: https://support.happyfox.com/kb/article/1201-api-for-asset-management/ (last updated 2026-08-07 08:21:36 UTC)

## Overview

API provided by the HappyFox helpdesk is a RESTful web service. It supports operations like creating a ticket, adding updates to a ticket, listing tickets and users of the Helpdesk. It supports JSON, Form Url encoded and Multipart Form Data formats as payload.

Requirements:

The API requires following skills in any programming language:

- Making HTTP requests (using GET and POST HTTP methods as a minimum requirement).
- Doing HTTP Basic Authentication.
- Generating and reading data in the JSON format.
- Optionally making HTTP POST requests using content type of "multipart/form-data" (needed for ticket attachments)

## Documentation Conventions

The documentation indicates parameters that need to be replaced with actual values using the format `<parameter>`. The entire string including the enclosing \< and > should be replaced.

For example, if the parameter email is `<email>` it should be replaced with the required email address

Endpoint url format:

`https://<account_name>.happyfox.com/api/1.1/`

> Note: If you are using a custom domain, please use the custom domain url API key and auth code are to be passed in Basic HTTP authentication format only.

A full constructed example url is shown below:

`https://customersupport.happyfox.com/api/1.1/json/assets/`

> If your HappyFox account is hosted in EU, please use `.happyfox.net`

API endpoints that are used to edit/delete assets use asset id. This is not same as the display ID or the asset name. This asset id is a non-zero integer and is a backend identifier of the asset. It can be obtained from the /assets/ endpoint

![](https://hf-files-oregon.s3.amazonaws.com/hdpsupport_kb_attachments/2026/05-18/59462bd0-56bd-48d5-9ac2-616cab46df34/ABC.png)

## Table of Contents

1. Read all assets
2. Read one asset by asset ID
3. Create asset
4. Update asset
5. Delete asset
6. Read all asset custom fields
7. Read one asset custom field by id
8. Read all asset types
9. Asset type details

---

## 1. Read all assets

This returns a paginated list of all assets in your HappyFox account.

API Endpoint url: `/api/1.1/json/assets/?asset_type=<asset_type_ID>&size=<size value>&page=<page number>`

HTTP Method: GET

| Parameter | Required | Description |
|---|---|---|
| size | No | Number of assets to return per page. Default page size is 10. Maximum allowed page size is 50 |
| page | No | Page Number |
| asset_type | Yes | ID of the asset type which is required. If this query parameter is not passed, assets belonging to the first asset type will be returned |

Sample results:

```json
{
  "page_info": {
    "count": 10,
    "last_index": 198,
    "page_count": 20,
    "start_index": 1,
    "end_index": 10
  },
  "data": [
    {
      "display_id": "2",
      "name": "21",
      "contacts": [],
      "contact_groups": [],
      "created_by": 1,
      "id": 659,
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "custom_fields": [
        {
          "name": "Serial Number",
          "type": "text",
          "order": 1,
          "value": null,
          "id": 45
        },
        {
          "name": "Manufacturer",
          "type": "text",
          "order": 2,
          "value": null,
          "id": 46
        },
        {
          "name": "Model",
          "type": "text",
          "order": 3,
          "value": null,
          "id": 47
        }
      ],
      "updated_by": 1
    },
    {
      "display_id": "ADDISD-W10PC",
      "name": "ADDISD-W10PC",
      "contacts": [
        {
          "email": "dwyane@example.com",
          "name": "Dwyane Wade",
          "id": 27662
        }
      ],
      "contact_groups": [
        {
          "id": 1,
          "name": "APAC Region"
        }
      ],
      "created_by": 1,
      "id": 6748,
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "custom_fields": [
        {
          "name": "Serial Number",
          "type": "text",
          "order": 1,
          "value": "GCJ1353",
          "id": 45
        },
        {
          "name": "Manufacturer",
          "type": "text",
          "order": 2,
          "value": "Dell",
          "id": 46
        },
        {
          "name": "Model",
          "type": "text",
          "order": 3,
          "value": "Latitude 5500",
          "id": 47
        }
      ],
      "updated_by": 1
    }
  ]
}
```

---

## 2. Read one asset by asset ID

Each asset and its details can be accessed using this API endpoint

API Endpoint: `/api/1.1/json/asset/<id>/`

HTTP Method: GET

Sample result:

```json
{
  "display_id": "ADDISD-W10PC",
  "name": "ADDISD-W10PC",
  "contacts": [
    {
      "email": "dwyane@example.com",
      "name": "Dwyane Wade",
      "id": 27662
    }
  ],
  "contact_groups": [
    {
      "id": 1,
      "name": "APAC Region"
    }
  ],
  "created_by": 1,
  "id": 6748,
  "asset_type": {
    "id": 1,
    "name": "General"
  },
  "custom_fields": [
    {
      "name": "Serial Number",
      "type": "text",
      "order": 1,
      "value": "GCJ1353",
      "id": 45
    },
    {
      "name": "Manufacturer",
      "type": "text",
      "order": 2,
      "value": "Dell",
      "id": 46
    },
    {
      "name": "Model",
      "type": "text",
      "order": 3,
      "value": "Latitude 5500",
      "id": 47
    }
  ],
  "updated_by": 1
}
```

---

## 3. Create asset

API Endpoint: `/api/1.1/json/assets/?asset_type=<asset_type_id>`

Query Parameter:

asset_type is the only mandatory parameter here. Please pass ID of the asset type in which this asset should be created. If this query parameter is not passed, asset will be created in the first asset type.

HTTP Method: POST

Payload fields:

| Field | Type | Is Required | Description |
|---|---|---|---|
| name | String | Yes | Name of the asset. Allows up to 200 characters |
| display_id | String | Yes | Display ID of the asset for the UI |
| contact_ids | List | No | IDs of existing contacts to be linked to this asset. Refer [this doc](https://support.happyfox.com/kb/article/1092-contacts-and-contact-groups-api-endpoints/#ListContacts) to fetch IDs of contacts |
| contact_group_ids | List | No | IDs of existing contact groups to be linked to this asset. Refer [this doc](https://support.happyfox.com/kb/article/1092-contacts-and-contact-groups-api-endpoints/#ListContactGroups) to fetch IDs of contact groups |
| contacts | List | No | This has to be a JSON array with the details of new contacts to be linked created, along with asset creation. Required fields for each element in the JSON array are name and email/phone number. For an agent to create contacts when creating or updating an asset, the agent should have Manage all Contacts permission |
| custom fields | JSON | No | JSON array of custom field and their values for this asset |
| created_by | Integer | Yes | ID of the agent that who is creating this asset. Refer [this article](https://support.happyfox.com/kb/article/360-api-for-happyfox/#:~:text=https%3A//%3Caccount_name%3E.happyfox.com/api/1.1/json/staff/) to see agents list in your HappyFox account |

Rules for specifying custom field values in request data:

| Custom Field Type | Value format | Example |
|---|---|---|
| Text | String | "Lorem ipsum" |
| Textarea | String | "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor" |
| Number | integer | 12 |
| Dropdown | ID of the option to be set | 1 |
| Multiple options | ID of options to be selected | [1,2,3] |
| Date | YYYY-MM-DD | 2020-12-25 |

Example payload:

```json
{
  "name": "Macbook pro v1",
  "display_id": "macbook_pro_v1",
  "contact_ids": ["1"],
  "contact_group_ids": ["1"],
  "contacts": [
    {"email": "john@example.com"}
  ],
  "created_by": 1,
  "custom_fields": {
    "1": "1",
    "2": "Macbook Pro - 2020",
    "3": "This is the most recent version of the available Macbook Pro",
    "4": "C034234ZHY",
    "5": 4,
    "6": [3, 4],
    "7": "2020-11-15"
  }
}
```

Example Successful Response (status code - 200)

```json
{
  "id": 13,
  "name": "Macbook Pro",
  "display_id": "mac_d11pro",
  "created_by": 1,
  "created_at": "2019-11-15T07:49:40Z",
  "updated_by": null,
  "updated_at": "2019-11-15T07:49:40Z",
  "contacts": [
    {
      "email": "john@example.com",
      "id": 1,
      "name": "John"
    },
    {
      "email": "donald@example.com",
      "id": 2,
      "name": "Donald"
    }
  ],
  "contact_groups": [
    {
      "id": 1,
      "name": "APAC region"
    }
  ],
  "custom_fields": [
    {
      "id": 1,
      "name": "Asset Category",
      "type": "dropdown",
      "value": 1,
      "order": 1
    },
    {
      "id": 2,
      "name": "Description",
      "type": "textarea",
      "value": null,
      "order": 2
    }
  ]
}
```

Example failure response in case of validation errors (status code - 400)

```json
{
  "error": {
    "display_id": [
      "This field is required."
    ],
    "contact_ids": "Enter a list of values.",
    "custom_fields": {
      "1": "Provide a valid choice. 5 is not one of the available choices."
    }
  }
}
```

---

## 4. Update asset

API Endpoint: `/api/1.1/json/asset/<id>/`

HTTP Method: PUT

Payload fields:

| Field | Type | Is Required? | Description |
|---|---|---|---|
| name | String | No | Name of the asset |
| display_id | string | No | Display ID of the asset for the UI |
| contact_ids | List | No | IDs of existing contacts to be linked to this asset. Refer [this doc](https://support.happyfox.com/kb/article/1092-contacts-and-contact-groups-api-endpoints/#ListContacts) to fetch IDs of contacts |
| contact_group_ids | List | No | IDs of existing contact groups to be linked to this asset. Refer [this doc](https://support.happyfox.com/kb/article/1092-contacts-and-contact-groups-api-endpoints/#ListContactGroups) to fetch IDs of contact groups |
| contacts | List | No | This has to be a JSON array with the details of new contacts to be linked, along with asset updating process. Required fields for each element in the JSON array are name and email/phone number. For an agent to create contacts when creating or updating an asset, the agent should have Manage all Contacts permission |
| custom fields | JSON | No | JSON array of custom field and their values for this asset |
| updated_by | Integer | Yes | ID of the agent that who is updating this asset. Refer [this article](https://support.happyfox.com/kb/article/360-api-for-happyfox/#:~:text=https%3A//%3Caccount_name%3E.happyfox.com/api/1.1/json/staff/) to see agents list in your HappyFox account |

Example Request

```json
{
  "name": "Macbook pro 2019 15' inch Retina",
  "updated_by": 1,
  "contact_ids": [2],
  "contact_group_ids": [1]
}
```

Example Successful Response (status code - 200)

```json
{
  "id": 2,
  "name": "Macbook pro 2019 15' inch Retina",
  "display_id": "mac_1pro",
  "created_by": 1,
  "created_at": "2019-11-15T06:43:51Z",
  "updated_by": 1,
  "updated_at": "2019-11-15T08:00:14Z",
  "contacts": [
    {
      "email": "john@example.com",
      "id": 2,
      "name": "John"
    }
  ],
  "contact_groups": [
    {
      "id": 1,
      "name": "APAC Region"
    }
  ],
  "asset_type": {
    "id": 1,
    "name": "General"
  },
  "custom_fields": [
    {
      "id": 1,
      "name": "Asset Category",
      "type": "dropdown",
      "value": 1,
      "order": 1
    },
    {
      "id": 2,
      "name": "Description",
      "type": "textarea",
      "value": null,
      "order": 2
    }
  ]
}
```

---

## 5. Delete asset

API Endpoint: `/api/1.1/json/asset/<asset id>/?deleted_by=<staff id>`

HTTP Method: DELETE

Request Query Parameters:

| Parameter | Is Required? | Description |
|---|---|---|
| deleted_by | Yes | ID of the agent who is making the request |

Only an active agent who has Manage Assets permission can delete an asset

---

## 6. Read all asset custom fields

API Endpoint: `/api/1.1/json/asset_custom_fields/?asset_type=<asset_type_ID>&size=<page_size>&page=<page_no>`

HTTP Method: GET

Request Query Parameters:

| Parameter | Is Required? | Description |
|---|---|---|
| size | No | Number of assets to return per page. Default page size is 10. Maximum allowed page size is 50 |
| page | No | Page Number |
| asset_type | Yes | ID of the asset type. If this query parameter is not passed, custom fields belonging to the first asset type will be returned |

Example Response (status code - 200)

```json
{
  "page_info": {
    "count": 5,
    "last_index": 5,
    "page_count": 1,
    "start_index": 1,
    "end_index": 5
  },
  "data": [
    {
      "id": 45,
      "type": "text",
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "name": "Serial Number",
      "choices": null
    },
    {
      "id": 46,
      "type": "text",
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "name": "Manufacturer",
      "choices": null
    },
    {
      "id": 47,
      "type": "text",
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "name": "Model",
      "choices": null
    },
    {
      "id": 56,
      "type": "number",
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "name": "Numbers acquired",
      "choices": null
    },
    {
      "id": 57,
      "type": "date",
      "asset_type": {
        "id": 1,
        "name": "General"
      },
      "name": "Warranty Date",
      "choices": null
    }
  ]
}
```

---

## 7. Read one asset custom field by id

API Endpoint: `/api/1.1/json/asset_custom_field/<id>/`

HTTP Method: GET

Example Response (status code - 200)

```json
{
  "id": 45,
  "type": "text",
  "asset_type": {
    "id": 1,
    "name": "General"
  },
  "name": "Serial Number",
  "choices": null
}
```

---

## 8. Read all asset types

API Endpoint: `/api/1.1/json/asset_types/`

HTTP Method: GET

Sample response:

```json
{
  "page_info": {
    "count": 2,
    "page_count": 1,
    "last_index": 2,
    "start_index": 1,
    "end_index": 2
  },
  "data": [
    {
      "description": null,
      "id": 1,
      "name": "General",
      "settings": {
        "enable_ticket_association": true,
        "enable_contact_association": true,
        "enable_asset_type_visibility_to_contact": true,
        "enable_asset_quantity_for_ticket": true
      }
    },
    {
      "description": "",
      "id": 2,
      "name": "Device",
      "settings": {
        "enable_ticket_association": true,
        "enable_contact_association": false,
        "enable_asset_type_visibility_to_contact": false,
        "enable_asset_quantity_for_ticket": false
      }
    }
  ]
}
```

---

## 9. Asset type details

API Endpoint: `/api/1.1/json/asset_type/<asset type id>/`

HTTP Method: GET

Example response:

```json
{
  "description": null,
  "id": 1,
  "name": "General",
  "settings": {
    "enable_ticket_association": true,
    "enable_contact_association": true,
    "enable_asset_type_visibility_to_contact": true,
    "enable_asset_quantity_for_ticket": true
  }
}
```
