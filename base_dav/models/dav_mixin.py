import uuid

import pytz
import vobject
from odoo import api, fields, models

# FIXME build list using vobject.knownChildren
_MULTI_VALUE = ["attendee", "categories", "comment", "attach"]


class DavMixin(models.AbstractModel):
    """
    Mixin to add DAV support to any Odoo model.
    Adds dav_uid for DAV UID and etag computation.
    """

    _name = "dav.mixin"
    _description = "DAV Mixin"
    _dav_field_uuid = "dav_uid"

    dav_uid = fields.Char(
        string="DAV UID",
        copy=False,
        index=True,
    )

    _sql_constraints = [
        ("dav_uid_unique", "UNIQUE(dav_uid)", "DAV UID must be unique!"),
    ]

    def init(self):
        """Initialize dav_uid for existing events."""
        if self._abstract:
            return
        self._cr.execute(f"""
            UPDATE {self._table}
            SET dav_uid = gen_random_uuid()::text
            WHERE dav_uid IS NULL
        """)

    @api.model_create_multi
    def create(self, vals_list):
        """Ensure every new record gets a unique DAV UID."""
        for vals in vals_list:
            if not vals.get(self._dav_field_uuid):
                vals[self._dav_field_uuid] = str(uuid.uuid4())
        return super().create(vals_list)

    # ---------------------------------------------------------------------------
    # Reusable helpers
    # ---------------------------------------------------------------------------

    def get_vobject_multi_value(self, component, prop_name: str) -> list:
        """
        Safely extract a flat list of values from a vobject component property
        that may appear multiple times and/or contain multiple comma-separated values.

        Handles:
        - Missing property                → []
        - Single line, single value       → ["A"]
        - Single line, multi value        → ["A", "B"]
        - Multiple lines                  → ["A", "B", "C"]
        - Mixed                           → ["A", "B", "C", "D"]
        """
        result = []
        for item in component.contents.get(prop_name, []):
            raw = item.value
            if isinstance(raw, list):
                result.extend(raw)
            else:
                result.append(raw)
        return result

    def extract_field_mapping_from_vobject(self, vobj, collection):
        """Extract Odoo field values from a vobject component using collection mapping.
        Does NOT write to any record.

        :param vobj: vobject component (VEVENT, VCARD, etc.)
        :param collection: DAV collection definition
        :return: dict of Odoo field values
        :rtype: dict
        """
        values = {}
        for mapping in collection.field_mapping_ids:
            if mapping.name.lower() in _MULTI_VALUE:
                child = vobj.contents.get(mapping.name.lower())
            else:
                child = vobj.contents.get(mapping.name.lower(), [None])[0]
            if not child:
                continue
            value = mapping.from_vobject(child)
            if value is not None:
                values[mapping.field_id.name] = value

        # Always extract UID if present and not already mapped via field_mapping_ids.
        uid_field = collection._get_uid_field_name()
        if uid_field and uid_field not in values:
            uid_child = vobj.contents.get("uid", [None])[0]
            if uid_child:
                values[uid_field] = uid_child.value

        return values

    def apply_values(self, record, values, collection):
        """Create or update a record with the given values.

        Performs a single write or create call.

        :param record: Existing Odoo record to update, or None to create
        :param values: dict of Odoo field values
        :param collection: DAV collection definition
        :return: Created or updated Odoo record
        :rtype: BaseModel
        """
        if not values:
            return record
        if record:
            record.write(values)
            return record
        return self.env[collection.model_id.model].create(values)

    def apply_field_mapping_to_vobject(self, vobj, record, collection):
        """Populate a vobject component from an Odoo record using collection mapping.

        Custom rules override hardcoded defaults: if a property is already present
        on a single-value field, it is replaced rather than duplicated.
        Multi-value fields are extended.

        Also ensures UID and REV/LAST-MODIFIED are always present.

        :param vobj: vobject component to populate (VEVENT, VCARD, etc.)
        :param record: Odoo record
        :param collection: DAV collection definition
        """
        for mapping in collection.field_mapping_ids:
            value = mapping.to_vobject(record)
            if value is None or value is False:
                continue
            if isinstance(value, bool):
                continue

            prop_name = mapping.name.lower()

            # Support list of values for unbounded fields e.g.: ATTENDEE
            if prop_name not in _MULTI_VALUE or not isinstance(value, list):
                value = [value]

            # Override: remove existing hardcoded property before applying custom
            # one, unless it is a multi-value property where we want to extend.
            if prop_name not in _MULTI_VALUE and prop_name in vobj.contents:
                del vobj.contents[prop_name]

            for subvalue in value:
                if isinstance(subvalue, (int, float)):
                    subvalue = str(subvalue)
                if isinstance(subvalue, tuple) and len(subvalue) == 2:
                    # Convert tuple to value & params
                    prop = vobj.add(mapping.name)
                    prop.value = subvalue[0]
                    prop.params = subvalue[1]
                else:
                    # Fallback to simple value
                    vobj.add(mapping.name).value = subvalue

        # Always ensure UID is present.
        if "uid" not in vobj.contents:
            vobj.add("uid").value = collection._get_record_uid_value(record)

        # Always ensure REV / LAST-MODIFIED is present.
        if "write_date" in record._fields and record.write_date:
            if collection.dav_type == "addressbook":
                field_name = "rev"
                date_value = record.write_date.strftime("%Y%m%dT%H%M%SZ")
            elif collection.dav_type == "calendar":
                field_name = "last-modified"
                date_value = record.write_date.replace(tzinfo=pytz.timezone("UTC"))
            else:
                field_name = None
                date_value = None
            if field_name and field_name not in vobj.contents:
                vobj.add(field_name).value = date_value

    # ----------------------------------------------------------------------
    # Public interface to implement
    # ----------------------------------------------------------------------

    def vobject_component_name(self):
        """
        Returns the vObject component type handled by this model.
        e.g. 'VEVENT', 'VCARD', 'VTODO'
        """
        raise NotImplementedError(
            f"Model '{self._name}' must implement 'vobject_component_name()'"
        )

    # ---------------------------------------------------------------------------
    # Main entry points
    # ---------------------------------------------------------------------------

    def from_vobject(self, item, record, collection):
        """Convert vobject item into Odoo field values, then create or update record.

        Supports:
        - VEVENT for calendar
        - VCARD for addressbook

        Subclasses should override this method to add hardcoded field extraction
        before calling super(), so that custom field mapping can override them.

        :param item: vobject instance
        :param record: Existing Odoo record to update, or None to create a new one
        :param collection: DAV collection definition
        :return: Created or updated Odoo record
        :rtype: BaseModel
        """
        if collection.dav_type == "calendar":
            if item.name != "VCALENDAR" or not hasattr(item, "vevent"):
                # FIXME We probably need to raise an error here.
                return None
            vobj = item.vevent
        elif collection.dav_type == "addressbook":
            if item.name != "VCARD":
                # FIXME We probably need to raise an error here.
                return None
            vobj = item
        # TODO Add new type for VTODO.
        else:
            return None

        # Custom mapping extraction. Subclasses should provide hardcoded values
        # before calling super() so that custom mapping can override them.
        values = self.extract_field_mapping_from_vobject(vobj, collection)

        # Single write or create.
        return self.apply_values(record, values, collection)

    def to_vobject(self, record, collection):
        """Convert Odoo record into vobject representation.

        Automatically adds:
          - UID if missing
          - REV based on write_date (addressbook)
          - LAST-MODIFIED based on write_date (calendar)

        Subclasses should override this method to add hardcoded properties
        before calling super(), so that custom field mapping can override them.

        :param record: Odoo record
        :param collection: DAV collection definition
        :return: vobject instance or None for unsupported types
        :rtype: Optional[Any]
        """
        if collection.dav_type == "calendar":
            result = vobject.iCalendar()
            # FIXME calendar supports VEVENT but also VTODO.
            vobj = result.add("vevent")
        elif collection.dav_type == "addressbook":
            result = vobject.vCard()
            vobj = result
        else:
            return None

        # Custom mapping overrides hardcoded properties set by subclasses.
        self.apply_field_mapping_to_vobject(vobj, record, collection)

        return result
