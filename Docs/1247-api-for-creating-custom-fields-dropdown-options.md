# API for creating custom fields dropdown options

Source: https://support.happyfox.com/kb/article/1247-api-for-creating-custom-fields-dropdown-options/ (last updated 2026-06-04 16:01:06 UTC)

HappyFox allows users to set dropdown values for ticket custom fields. These dropdown values will be maintained in an external source and will be fetched and displayed into the respective ticket custom field. Any additions/deletions/modifications done to the values will be reflected accordingly.

> Note: All timestamps in API responses are in UTC

This is achieved using the HappyFox API as follows:

API Endpoint: api/1.1/json/ticket_custom_field/\<ticket_custom_field_id>/

Method: PUT

Query parameters that can be passed to the url are listed below with their description

Note: Only choices property of ticket custom field can be updated with V1 API

| Params | Description |
|---|---|
| choices | List of choices for Drop down/Multiple choice field |

Example Payload

Let's consider the following case for updating the ticket custom fields choices

- “Option 1” - no change
- “Option 2 (Edited)” - edited
- “Option 3” - existing option that need to be deleted
- “Option 4” - new option

Note: “Option 3” which is to be deleted is not present in the below payload

```json
{
    "choices": [
            {
                "id": 11,
                "text": "Option 1",
                "dependant_fields": []

            },
            {
                "id": 12,
                "text": "Option 2 (Edited)",
                "dependant_fields": []
            },
            {
                "id": null,
                "text": "Options 4",
                "dependant_fields": []
            }
        ]
}
```

Example Response

```json
{
        "name": "Drop Down Field",
        "depends_on_choice": null,
        "required": true,
        "compulsory_on_completed": true,
        "choices": [
            {
                "text": "Option 1",
                "id": 11,
                "dependant_fields": []
            },
            {
                "text": "Option 2 (Edited)",
                "id": 12,
                "dependant_fields": []
            },
            {
                "text": "Option 4",
                "id": 13,
                "dependant_fields": []
            }
        ],
        "compulsory_on_move": true,
        "type": "choice",
        "id": 1,
        "categories": [
            {
                "category": 1,
                "order": 1
            }
        ],
        "visible_to_staff_only": false
    }
```
