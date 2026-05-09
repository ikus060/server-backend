# Copyright 2019 Therp BV <https://therp.nl>
# Copyright 2019-2020 initOS GmbH <https://initos.com>
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import posixpath
import time
from hashlib import blake2b
from operator import itemgetter
from urllib.parse import quote_plus, unquote_plus

import vobject
from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.osv import expression
from odoo.tools.safe_eval import safe_eval

from ..controllers.main import PREFIX

_DAV_ITEM_EXTENSIONS = (".vcf", ".ics")


def _dav_strip_item_extension(name: str) -> str:
    """Return DAV item identifier without common extensions (.vcf/.ics)."""
    name = (name or "").strip()
    lower = name.lower()
    for ext in _DAV_ITEM_EXTENSIONS:
        if lower.endswith(ext):
            return name[: -len(ext)]
    return name


class DavCollection(models.Model):
    _name = "dav.collection"
    _description = "A collection accessible via WebDAV"

    name = fields.Char(required=True)
    rights = fields.Selection(
        [
            ("owner_only", "Owner Only"),
            ("owner_write_only", "Owner Write Only"),
            ("authenticated", "Authenticated"),
        ],
        required=True,
        default="owner_only",
    )
    dav_type = fields.Selection(
        [
            ("calendar", "Calendar"),
            ("addressbook", "Addressbook"),
            # TODO Add VTODO.
            ("files", "Files"),
        ],
        string="Type",
        required=True,
        default="calendar",
    )
    tag = fields.Char(compute="_compute_tag")
    model_name = fields.Char(
        string="Model Technical Name",
        related="model_id.model",
    )
    model_id = fields.Many2one(
        "ir.model",
        required=True,
        domain=[("transient", "=", False)],
        ondelete="cascade",
    )
    domain = fields.Char(
        string="Domain Filter",
        required=True,
        default="[]",
        help=(
            "Odoo domain to filter records. "
            "Use ${user.id} or ${user.login} as placeholders for the current user."
        ),
    )
    field_uuid = fields.Many2one(
        "ir.model.fields",
        string="UID Field",
        help=(
            "Select the field to be used as the unique identifier (UID) "
            "for CalDAV/CardDAV synchronization mapping. "
            "This field must contain a unique value per record "
            "to ensure proper synchronization."
        ),
    )
    field_mapping_ids = fields.One2many(
        "dav.collection.field_mapping",
        "collection_id",
        string="Field mappings",
    )
    url = fields.Char(compute="_compute_url")
    calendar_color = fields.Char(default="#48c9f4")
    description = fields.Text(string="Description")
    mapper_mode = fields.Selection(
        string="Mapping Mode",
        selection="_selection_mapper_mode",
        required=True,
        default="custom",
        help=(
            "Custom: Use field mappings defined manually.\n"
            "Built-in: Use predefined mapper for known models (res.partner, calendar.event).\n"
        ),
    )

    @api.depends("dav_type")
    def _compute_tag(self):
        """Compute DAV collection tag based on its type.

        Sets ``tag`` field to a DAV-specific container name:
          - ``VCALENDAR`` for calendar
          - ``VADDRESSBOOK`` for addressbook
          - False for files
        """
        for rec in self:
            if rec.dav_type == "calendar":
                rec.tag = "VCALENDAR"
            elif rec.dav_type == "addressbook":
                rec.tag = "VADDRESSBOOK"
            else:
                rec.tag = False

    def _selection_mapper_mode(self):
        """
        Build mapper mode choices dynamically from the registered mapper registry.
        Always includes 'custom'. Adds 'builtin' only when at least one
        built-in mapper is registered for the current dav_type.
        """
        # Default for backward compatibility.
        selections = [("custom", "Custom")]
        # Lookup for dav.mixin subclass.
        for model_name, model in self.env.registry.models.items():
            if (
                issubclass(model, self.env.registry["dav.mixin"])
                and model is not self.env.registry["dav.mixin"]
            ):
                selections.append((model_name, self.env[model_name]._description))
        return selections

    def _get_mapper(self):
        # Return the proper class to run the mapping.
        if self.mapper_mode and self.mapper_mode != "custom":
            try:
                return self.env[self.mapper_mode]
            except KeyError:
                # In case the module was uninstalled.
                pass
        # Fallback to dav mixin.
        return self.env["dav.mixin"]

    def _apply_mapper_defaults(self, vals):
        """Apply mapper defaults to vals dict, used in create/write."""
        mapper_mode = vals.get("mapper_mode")
        if not mapper_mode or mapper_mode == "custom":
            return vals
        # Get the model
        try:
            model = self.env[mapper_mode]
        except KeyError:
            # In case module was uninstalled.
            return vals

        # Get the model
        model_id = self.env["ir.model"].search([("model", "=", mapper_mode)], limit=1)
        if not model_id:
            return vals
        vals["model_id"] = model_id.id

        # Get dav type from Model
        if getattr(model, "_dav_type", False):
            vals["dav_type"] = model._dav_type

        # Get UID field from Model
        if getattr(model, "_dav_field_uuid", False):
            field = self.env["ir.model.fields"].search(
                [
                    ("model_id", "=", model_id.id),
                    ("name", "=", model._dav_field_uuid),
                ],
                limit=1,
            )
            vals["field_uuid"] = field.id if field else False

        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._apply_mapper_defaults(vals) for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        vals = self._apply_mapper_defaults(vals)
        return super().write(vals)

    def _compute_url(self):
        """Compute absolute DAV access URL for the collection.

        URL is constructed using:
          - system base URL
          - DAV prefix
          - current user login
          - collection ID
        """
        base_url = (
            self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        ).rstrip("/")
        login = self.env.user.login
        for rec in self:
            rec.url = f"{base_url}{PREFIX}/{login}/{rec.id}"

    @api.constrains("domain")
    def _check_domain(self):
        """Validate domain expression.

        Ensures that the stored domain string can be safely evaluated.

        :raises Exception: If domain evaluation fails
        """
        for rec in self:
            rec._eval_domain()

    @api.model
    def _eval_context(self):
        """Return safe evaluation context for domain expressions.

        :return: Dictionary containing available evaluation variables
        :rtype: Dict[str, Any]
        """
        return {
            "user": self.env.user,
        }

    def _eval_domain(self):
        """Evaluate stored domain expression, merged with mapper default domain.

        :raises ValueError: If domain string is invalid
        :return: Evaluated domain
        :rtype: List[Any]
        """
        self.ensure_one()
        user_domain = list(safe_eval(self.domain or "[]", self._eval_context()))

        mapper = self._get_mapper()
        default_domain_str = getattr(mapper, "_dav_default_domain", None)
        if default_domain_str:
            return expression.AND([user_domain, default_domain_str])

        return user_domain

    def eval_domain_records(self):
        """Search records matching the evaluated domain.

        :return: Recordset of matching records
        :rtype: odoo.models.BaseModel
        """
        self.ensure_one()
        model_name = self.sudo().model_id.model
        return self.env[model_name].search(self._eval_domain())

    def get_record(self, components):
        """Retrieve record from path components.

        :param components: Parsed DAV path components
        :type components: Sequence[str]

        :return: Matching record or empty recordset
        :rtype: odoo.models.BaseModel
        """
        self.ensure_one()
        model_name = self.sudo().model_id.model
        collection_model = self.env[model_name]
        raw_key = components[-1] if components else ""
        key = _dav_strip_item_extension(raw_key)
        field_uuid = self.sudo().field_uuid
        if field_uuid:
            field_name = field_uuid.name
            if field_uuid.ttype in ("integer", "many2one"):
                try:
                    key = int(key)
                except (TypeError, ValueError):
                    return collection_model.browse()
        else:
            field_name = "id"
            try:
                key = int(key)
            except (TypeError, ValueError):
                return collection_model.browse()

        domain = expression.AND(
            [
                [(field_name, "=", key)],
                self._eval_domain(),
            ]
        )
        return collection_model.search(domain, limit=1)

    def _get_uid_field_name(self):
        """Return the Odoo field name used as the DAV UID for this collection.

        Uses ``field_uuid`` when configured, otherwise falls back to ``id``.

        :rtype: str
        """
        self.ensure_one()
        if self.field_uuid:
            return self.field_uuid.name
        return "id"

    def _get_record_uid_value(self, record):
        """Return the DAV item identifier for a record.

        Uses ``field_uuid`` when configured, otherwise falls back to ``record.id``.
        The returned value matches the identifier format expected by
        :meth:`get_record`.

        :param record: Odoo record
        """
        self.ensure_one()

        field_name = self._get_uid_field_name()
        value = record[field_name]

        if self.field_uuid and self.field_uuid.ttype == "many2one":
            return str(value.id) if value else ""
        return str(value)

    def from_vobject(self, item, record):
        return self._get_mapper().from_vobject(item, record, self)

    def to_vobject(self, record):
        return self._get_mapper().to_vobject(record, self)

    @api.model
    def _odoo_to_http_datetime(self, value):
        """Convert Odoo datetime to HTTP-date format (RFC 7231).

        :param value: Datetime value (string or datetime)
        :type value: Any

        :return: HTTP formatted datetime string or None
        :rtype: Optional[str]
        """
        if not value:
            return None
        if not isinstance(value, str):
            value = fields.Datetime.to_string(value)
        return time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT",
            time.strptime(value, "%Y-%m-%d %H:%M:%S"),
        )

    @api.model
    def _split_path(self, path):
        """Split DAV path into normalized components.

        :param path: Raw path string
        :type path: Optional[str]

        :return: List of path segments
        :rtype: List[str]
        """
        return [
            part
            for part in posixpath.normpath(f"/{path or ''}").strip("/").split("/")
            if part
        ]

    def dav_etag(self) -> str:
        """
        Return a ETAG for to represent this collection.
        """
        self.ensure_one()

        # Use blake2b, because it's fast.
        h = blake2b(digest_size=16)

        # Compute hash using id + write_date for all record- including the collection it self.
        h.update(bytes(self.id))
        h.update(str(self.write_date).encode())

        collection_model = self.env[self.model_id.model]
        rows = collection_model.search_read(
            self._eval_domain(),
            fields=["id", "write_date"],
            order="id asc",  # stable ordering
        )
        for row in rows:
            h.update(bytes(row["id"]))
            h.update(str(row["write_date"]).encode())

        return '"%s"' % h.hexdigest()

    def dav_list(
        self,
        collection,
        path_components,
    ):
        """List DAV resources under given path.

        Handles:
          - file collections (attachments)
          - record-based collections (calendar/addressbook)

        :param collection: Radicale collection instance
        :type collection: Any
        :param path_components: Parsed DAV path
        :type path_components: Sequence[str]

        :return: List of resource href paths
        :rtype: List[str]
        """
        self.ensure_one()

        if self.dav_type == "files":
            if len(path_components) == 3:
                model_name = self.sudo().model_id.model
                collection_model = self.env[model_name]
                folder_name = unquote_plus(path_components[2])
                record = collection_model.browse(
                    map(
                        itemgetter(0),
                        collection_model.name_search(
                            folder_name,
                            operator="=",
                            limit=1,
                        ),
                    )
                )
                return [
                    "/"
                    + "/".join((*path_components, quote_plus(attachment.name or "")))
                    for attachment in self.env["ir.attachment"].search(
                        [
                            ("type", "=", "binary"),
                            ("res_model", "=", record._name),
                            ("res_id", "=", record.id),
                        ]
                    )
                ]
            elif len(path_components) == 2:
                return [
                    "/" + "/".join((*path_components, quote_plus(record.display_name)))
                    for record in self.eval_domain_records()
                ]

        if len(path_components) > 2:
            return []

        result = []
        for record in self.eval_domain_records():
            result.append(
                "/" + "/".join((*path_components, self._get_record_uid_value(record)))
            )
        return result

    def dav_delete(
        self,
        collection,
        href,
    ):
        """Delete DAV resource by href.

        :param collection: Radicale collection instance
        :type collection: Any
        :param href: Resource path
        :type href: str
        """
        self.ensure_one()

        if self.dav_type == "files":
            # TODO: Handle deletion of attachments
            return

        components = self._split_path(href)
        rec = self.get_record(components)
        if not rec:
            return

        # Prefer soft-delete (archive) if the model supports it
        rec.with_context(archive_on_error=True, dav_delete=True).unlink()

    def dav_upload(self, collection, href, item):
        """Create or update DAV resource from uploaded vobject.

        :param collection: Radicale collection instance
        :type collection: Any
        :param href: Resource path
        :type href: str
        :param item: Uploaded vobject
        :type item: Any
        :raises AccessError: If created/updated record is outside collection domain
        :return: Radicale Item instance or None
        :rtype: Optional[Any]
        """
        self.ensure_one()

        if self.dav_type == "files":
            # TODO: Handle upload of attachments
            return None

        components = self._split_path(href)

        # Get corresponding mapper.
        mapper = self._get_mapper()

        # TODO We should let the mapper customize the domain here.
        record = self.get_record(components)

        # Create or update record from vobject.
        record = mapper.from_vobject(item, record=record, collection=self)

        # Before returning this record, check if part of the domain.
        collection_model = self.env[self.model_id.model]
        domain = expression.AND([self._eval_domain(), [("id", "=", record.id)]])
        if not collection_model.search(domain, limit=1):
            raise AccessError(self.env._("Record is outside of DAV collection domain"))

        from ..radicale.collection import Item as DavItem

        return DavItem(
            collection,
            item=mapper.to_vobject(record, self),
            href=href,
            last_modified=self._odoo_to_http_datetime(record.write_date),
        )

    def dav_get(self, collection, href):
        """Retrieve DAV resource.

        Supports:
          - Folder access (files)
          - Attachment download
          - Calendar/addressbook items

        :param collection: Radicale collection instance
        :type collection: Any
        :param href: Resource path
        :type href: str

        :return: Radicale Item/FileItem/Collection or None
        :rtype: Optional[Any]
        """
        self.ensure_one()

        components = self._split_path(href)
        model_name = self.sudo().model_id.model
        collection_model = self.env[model_name]
        if self.dav_type == "files":
            if len(components) == 3:
                from ..radicale.collection import Collection as DavFolder

                folder = DavFolder(href)
                return folder

            if len(components) == 4:
                folder_name = unquote_plus(components[2])
                record = collection_model.browse(
                    map(
                        itemgetter(0),
                        collection_model.name_search(
                            folder_name,
                            operator="=",
                            limit=1,
                        ),
                    )
                )
                att_name = unquote_plus(components[3])
                attachment = self.env["ir.attachment"].search(
                    [
                        ("type", "=", "binary"),
                        ("res_model", "=", record._name),
                        ("res_id", "=", record.id),
                        ("name", "=", att_name),
                    ],
                    limit=1,
                )
                if not attachment:
                    return None

                from ..radicale.collection import FileItem

                return FileItem(
                    collection,
                    href,
                    attachment,
                    # TODO I think _odoo_to_http_datetime should be moved to Item class.
                    last_modified=self._odoo_to_http_datetime(record.write_date),
                )

        record = self.get_record(components)

        if not record:
            return None

        from ..radicale.collection import Item as DavItem

        mapper = self._get_mapper()

        return DavItem(
            collection,
            item=mapper.to_vobject(record, self),
            href=href,
            last_modified=self._odoo_to_http_datetime(record.write_date),
        )
