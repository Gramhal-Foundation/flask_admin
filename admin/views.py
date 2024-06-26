"""
Admin Blueprint Views

This module contains route handlers and views for the
Flask Blueprint named 'admin'.
The blueprint handles administrative routes and related functionality.

Dependencies:
    - Flask: Flask framework for web development.
    - Flask-Login: Extension for handling user authentication and sessions.
    - Flask-WTF: Extension for handling forms and form validation.
    - Other imported modules and classes (details in the import statements).

Routes and Functionality:
    - /: Redirects to the dashboard route.
    - /dashboard: Displays the admin dashboard.
    - /login: Handles user login and authentication.
    - /logout: Logs out the current user.
    - /resource/<string:resource_type>: Lists resources of a given type.
    - /resource/<string:resource_type>/create: Creates a new resource of
    the specified type.
    - /resource/<string:resource_type>/<string:resource_id>/edit: Edits an
    existing resource.
    - /resource/<string:resource_type>/<string:resource_id>/delete: Deletes an
    existing resource.
    - /resource/<string:resource_type>/download: Downloads resources as CSV.
    - /resource/<string:resource_type>/download-sample: Downloads a sample CSV
    for resource upload.
    - /resource/<string:resource_type>/upload: Handles resource
    upload from CSV.

Views:
    - 'index': Redirects to the dashboard.
    - 'dashboard': Displays the admin dashboard.
    - 'login': Handles user login.
    - 'logout': Logs out the user.
    - 'resource_list': Lists resources of a given type.
    - 'resource_create': Creates a new resource.
    - 'resource_edit': Edits an existing resource.
    - 'resource_delete': Deletes an existing resource.
    - 'resource_download': Downloads resources as CSV.
    - 'resource_download_sample': Downloads a sample CSV for resource upload.
    - 'resource_upload': Handles resource upload from CSV.

Custom Template Filters:
    - 'admin_label_plural': Returns the plural form of a label.
    - 'admin_format_datetime': Formats a datetime object.
    - 'format_label': Formats a label by replacing underscores with spaces.
"""

import ast
import copy
import csv
import io
import string
from datetime import datetime

import boto3
import inflect
import pandas as pd
from admin_view import *  # noqa: F401, F403
from admin_view import admin_configs
from bolbhavPlus.utils.sale_receipt import process_team_member_validation
from bolbhavPlus.utils.sale_receipt_controller import (
    update_approval_status,
    update_extracted_receipt_data,
)

# [TODO]: dependency on main repo
from db import db
from flask import Response
from flask import current_app as app
from flask import flash, redirect
from flask import render_template as real_render_template
from flask import request, url_for
from flask_bcrypt import Bcrypt
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm

# TODO: remove project dependency
from models.crop import CropModel
from models.mandi import MandiModel
from models.salesReceipt import ReceiptRejectionReason, SaleReceiptModel
from models.user import UserModel
from sqlalchemy import Text, and_, cast, func, or_
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename
from wtforms import PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired

from . import admin

bcrypt = Bcrypt()


def get_user_model_config():
    return admin_configs["user"]


def upload_file_to_s3(file, bucket_name="", acl="public-read"):
    """
    Docs: http://boto3.readthedocs.io/en/latest/guide/s3.html
    """
    if not app.config["S3_KEY"]:
        return None

    if not bucket_name:
        bucket_name = app.config["S3_BUCKET"]

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=app.config["S3_KEY"],
        aws_secret_access_key=app.config["S3_SECRET"],
    )
    try:
        s3_client.upload_fileobj(
            file,
            bucket_name,
            file.filename,
            ExtraArgs={"ACL": acl, "ContentType": file.content_type},
        )
        return "{}{}".format(app.config["S3_LOCATION"], file.filename)
    except Exception as e:
        print("Something Happened: ", e)
        return e


@admin.app_template_filter("admin_label_plural")
def admin_label_plural(label):
    """
    Custom template filter to convert a label into its plural form.

    Args:
        label (str): The label to be converted to its plural form.

    Returns:
        str: The plural form of the input label.
    """

    p = inflect.engine()
    formatted_label = label.replace("-", " ")
    formatted_label = p.plural_noun(formatted_label)
    formatted_label = string.capwords(formatted_label)
    return formatted_label


@admin.app_template_filter("admin_label_singular")
def admin_label_singular(label):
    formatted_label = label.replace("-", " ")
    formatted_label = string.capwords(formatted_label)
    return formatted_label


@admin.app_template_filter("admin_format_datetime")
def admin_format_datetime(value, format="%Y-%m-%d"):
    return datetime.strftime(value, format) if value else value


@admin.app_template_filter("admin_round_datetime")
def admin_round_datetime(value, round_to="second"):
    if value is None:
        return None
    if round_to == "second":
        return value.replace(microsecond=0)
    else:
        return value


@admin.app_template_filter("process_user_id")
def process_user_id(user_id):
    if user_id is None:
        return False

    selected_user = UserModel.query.filter(
        UserModel.roles == "cs_user", UserModel.id == user_id
    ).first()

    if selected_user is not None:
        if selected_user.roles == "cs_user":
            return "Team Member"
        else:
            return "Regular User"
    else:
        return "Regular User"


@admin.app_template_filter("check_price_range")
def check_price_range(price, min_price, max_price):
    if (
        min_price is None
        or max_price is None
        or min_price == 0
        or max_price == 0
    ):
        return "This is first receipt in Mandi"

    if price < min_price:
        return "The Rate is below min rate"
    elif price > max_price:
        return "The Rate is exceeding the max rate"
    else:
        return ""


@admin.app_template_filter("format_label")
def format_label(value):
    """
    Custom template filter to format a label string.

    Args:
        value (str): The label string to be formatted.

    Returns:
        str: The formatted label string with underscores replaced by spaces.
    """

    return value.replace("_", " ")


@admin.app_template_filter("get_nested_value")
def get_nested_value(resource, key_string):
    keys = key_string.split(".")
    current = resource

    try:
        for key in keys:
            current = getattr(current, key)
        return current
    except (KeyError, TypeError):
        return None


def get_resource_class(resource_type):
    class_names = get_class_names("admin_view.py")
    class_names.remove("FlaskAdmin")
    for x in class_names:
        if globals()[x].name == resource_type:
            return globals()[x]
    return None


def get_class_names(file_path):
    """
    Retrieve a list of class names from a Python file.

    This function parses the given Python file using the
    abstract syntax tree (AST) to extract the names of all
    class definitions present in the file.

    Args:
        file_path (str): The path to the Python file to be parsed.

    Returns:
        list: A list of class names present in the file.
    """

    with open(file_path, "r", encoding="utf-8") as file:
        file_content = file.read()

    class_names = []

    # Parse the Python code into an abstract syntax tree (AST)
    tree = ast.parse(file_content)

    # Traverse the AST and extract class names
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            class_names.append(node.name)

    return class_names


def get_resource_pk(resource_type):
    resource_class = get_resource_class(resource_type)
    resource_obj = resource_class()
    if hasattr(resource_obj, "pk"):
        return resource_obj.pk
    return "id"


def render_template(*args, **kwargs):
    """
    Custom template rendering function with extended attributes.

    This function is an extension of Flask's `render_template` function.
    It provides additional attributes to the template context,
    including a list of resource types and their associated permissions.

    Args:
        *args: Variable positional arguments for the
        template rendering function.
        **kwargs: Variable keyword arguments for
        the template rendering function.

    Returns:
        str: The rendered template with extended context attributes.
    """

    class_names = get_class_names("admin_view.py")
    class_names.remove("FlaskAdmin")
    resource_types = [globals()[x].name for x in class_names]
    template_attributes = {"resource_types": resource_types}
    template_attributes["permissions"] = {}
    for resource_type in resource_types:
        resource_class = get_resource_class(resource_type)
        resource_obj = resource_class()
        resource_permissions = {  # default permissions
            "create": False,
            "read": True,
            "update": False,
            "delete": False,
            "export": False,
            "import": False,
        }
        if hasattr(resource_obj, "permissions"):
            resource_permissions = resource_obj.permissions
        template_attributes["permissions"][
            resource_type
        ] = resource_permissions

    if "resource_type" in kwargs:
        original_pk = get_resource_pk(kwargs["resource_type"])

        if "pagination" in kwargs:
            for index, item in enumerate(kwargs["pagination"].items):
                setattr(
                    kwargs["pagination"].items[index],
                    "pk",
                    getattr(item, original_pk),
                )

        if "resource" in kwargs:
            setattr(
                kwargs["resource"],
                "pk",
                getattr(kwargs["resource"], original_pk),
            )

    return real_render_template(*args, **kwargs, **template_attributes)


def get_readable_attributes(resource_type):
    resource_class = get_resource_class(resource_type)
    model = resource_class.model

    ignore_columns = model.__table__.primary_key.columns.keys()

    model_attributes = []
    for column in model.__table__.columns:
        model_attributes.append(
            {"name": str(column.name), "type": str(column.type)}
        )

    readable_attributes = []
    for attribute in model_attributes:
        if attribute["name"] not in ignore_columns:
            readable_attributes.append(attribute)

    return readable_attributes


def get_editable_attributes(resource_type):
    resource_class = get_resource_class(resource_type)
    model = resource_class.model

    primary_key_columns = model.__table__.primary_key.columns.keys()
    ignore_columns = ["created_at", "updated_at"] + primary_key_columns
    if hasattr(resource_class, "protected_attributes"):
        ignore_columns = resource_class.protected_attributes + ignore_columns

    model_attributes = []
    for column in model.__table__.columns:
        model_attributes.append(
            {"name": str(column.name), "type": str(column.type)}
        )

    editable_attributes = []
    for attribute in model_attributes:
        if attribute["name"] not in ignore_columns:
            editable_attributes.append(attribute)

    return editable_attributes


def validate_resource_attribute(resource_type, attribute, initial_value):
    attribute_value = None
    if (
        "VARCHAR" in attribute["type"]
        or attribute["type"] == "TEXT"
        or attribute["type"] == "JSON"
    ):
        attribute_value = initial_value if initial_value else None
    elif "INT" in attribute["type"] or attribute["type"] == "FLOAT":
        attribute_value = initial_value if initial_value else None
    elif attribute["type"] == "DATE" or attribute["type"] == "DATETIME":
        attribute_value = initial_value if initial_value else None
    elif attribute["type"] == "BOOLEAN":
        if not isinstance(initial_value, bool):
            attribute_value = None
            if initial_value.lower() == "true":
                attribute_value = True
            elif initial_value.lower() == "false":
                attribute_value = False
        else:
            attribute_value = bool(initial_value)

    return attribute_value


class LoginForm(FlaskForm):
    """
    Form class for user login.

    This class defines a FlaskForm for handling user login.
    It includes fields for email and password,
    both of which are required for successful form submission.

    Attributes:
        email (StringField): The field for entering the user's email.
        password (PasswordField): The field for entering the user's password.
        submit (SubmitField): The field for submitting the login form.
    """

    phone = StringField("Phone", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Login")


@admin.route("/")
@login_required
def index():
    """
    Redirects to the dashboard route.

    This route handler function redirects the user to the 'dashboard' route.
    The user must be logged in to access this route.

    Returns:
        werkzeug.wrappers.response.Response: A redirection response
        to the dashboard route.
    """
    default_route = url_for(".dashboard")
    if "default-route-resource" in admin_configs:
        default_route = url_for(
            ".resource_list",
            resource_type=admin_configs["default-route-resource"],
        )
    return redirect(default_route)


@admin.route("/dashboard")
@login_required
def dashboard():
    """
    Displays the admin dashboard.

    This route handler function renders the 'dashboard.html' template,
    which displays the admin dashboard. The user must be
    logged in to access this route.

    Returns:
        str: The rendered dashboard template.
    """
    return render_template("dashboard.html")


@admin.route("/login", methods=["GET", "POST"])
def login():
    """
    Handles user login and authentication.

    This route handler function handles both GET and POST requests
    for user login. If a POST request is received with valid form data,
    the user's email and password are validated against the database.
    If the credentials are valid, the user is logged in and redirected to
    the dashboard. If the credentials are invalid, an error message
    is flashed to the user.

    Returns:
        str: The rendered 'login.html' template with the login form.
            If a POST request with invalid credentials is received,
            the template will display the error message in
            the flashed message area.
        werkzeug.wrappers.response.Response: A redirection response
        to the dashboard route if the user's credentials are valid.
    """
    form = LoginForm()
    if form.validate_on_submit():
        # username/phone & password to be picked from app config
        # to understand the primary field names and avoid conflicts
        phone = form.phone.data
        password = form.password.data
        user_model_config = get_user_model_config()
        user_model = user_model_config["model"]
        identifier = user_model_config["identifier"]
        user = user_model.query.filter(
            getattr(user_model, identifier) == phone
        ).first()
        bcrypt.init_app(app)
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            default_route = url_for(".dashboard")
            if user.roles == "data_extractor_intern":
                default_route = url_for(
                    ".resource_list",
                    resource_type="extract-data",
                )
                return redirect(default_route)

            if "default-route-resource" in admin_configs:
                default_route = url_for(
                    ".resource_list",
                    resource_type=admin_configs["default-route-resource"],
                )
            return redirect(default_route)
        else:
            flash("Invalid credentials. Please try again.", "error")

    return render_template("login.html", form=form)


@admin.route("/logout")
@login_required
def logout():
    """
    Logs out the current user.

    This route handler function logs out the currently
    logged-in user and redirects them to the 'login' route.

    Returns:
        werkzeug.wrappers.response.Response: A redirection
        response to the login route.
    """
    logout_user()
    return redirect(url_for(".login"))


def filter_resources(
    resource_class,
    model,
    list_display,
    search_params,
    page,
    per_page,
    sort=None,
):
    primary_key_column = model.__table__.primary_key.columns.keys()[0]
    filter_query = model.query
    search_query = search_params["search_query"]
    search_query_conditions = []
    if search_query:
        for column_name in list_display:
            if "." in column_name:
                sub_attributes = column_name.split(".")
                related_attribute = sub_attributes[0]
                related_model_column = sub_attributes[1]
                related_model = getattr(
                    model, related_attribute
                ).property.mapper.class_
                search_query_conditions.append(
                    cast(
                        getattr(related_model, related_model_column), Text
                    ).ilike(f"%{search_query}%")
                )
            else:
                column = model.__table__.columns.get(column_name)
                if column is not None:
                    search_query_conditions.append(
                        cast(column, Text).ilike(f"%{search_query}%")
                    )

    from_date = search_params["from_date"]
    to_date = search_params["to_date"]
    date_conditions = []
    date_field = getattr(resource_class, "searchable_date_field", "created_at")
    if from_date:
        date_conditions.append(
            func.date(getattr(model, date_field))
            >= func.date(datetime.strptime(from_date, "%Y-%m-%d"))
        )
    if to_date:
        date_conditions.append(
            func.date(getattr(model, date_field))
            <= func.date(datetime.strptime(to_date, "%Y-%m-%d"))
        )

    filter_query = filter_query.filter(
        and_(or_(*search_query_conditions), and_(*date_conditions))
    )

    if model.__name__ == "UserModel":
        role_condition = model.roles.in_(
            ["cs_user", "admin", "user", "superadmin"]
        )
        filter_query = filter_query.filter(
            or_(role_condition, model.roles.isnot(None))
        )

    if sort and len(sort):
        sort_conditions = []
        for criterion in sort:
            column_name = criterion["sort_by"]
            sort_order = criterion["sort_order"].lower()
            column = getattr(model, column_name)
            if sort_order == "desc":
                sort_conditions.append(column.desc())
            else:
                sort_conditions.append(column.asc())
        filter_query = filter_query.order_by(*sort_conditions)
    else:
        filter_query = filter_query.order_by(primary_key_column)

    # check for joins
    joinedload_statements = []
    for attribute in list_display:
        if "." not in attribute:
            continue
        sub_attributes = attribute.split(".")
        related_attribute = sub_attributes[0]
        joinedload_statements.append(
            joinedload(getattr(model, related_attribute))
        )
        filter_query = filter_query.join(getattr(model, related_attribute))

    if len(joinedload_statements):
        filter_query = filter_query.options(*joinedload_statements)

    return filter_query.paginate(page=page, per_page=per_page, error_out=False)


@admin.route("/resource/<string:resource_type>", methods=["GET", "POST"])
@login_required
def resource_list(resource_type):
    """
    Lists resources of a specific type.

    This route handler function displays a paginated list of resources
    of the given resource type. The resources are retrieved from the
    corresponding model class. The list is paginated, and the number
    of items per page is determined by the 'per_page' variable.

    Args:
        resource_type (str): The type of resource to list.

    Returns:
        str: The rendered 'resource/list.html' template with the paginated list
        of resources, including the pagination controls and relevant
        information about the resource type and list display attributes.
    """

    resource_class = get_resource_class(resource_type)
    model = resource_class.model

    hide_search = getattr(resource_class, "hide_search", False)
    hide_date_filter = getattr(resource_class, "hide_date_filter", False)

    if hasattr(resource_class, "admin_sale_receipt_controller"):
        status = request.args.get("status", default="pending")
        return resource_class.admin_sale_receipt_controller(
            resource_type, status
        )

    if hasattr(resource_class, "sale_receipt_data_extract_controller"):
        return resource_class.sale_receipt_data_extract_controller(
            resource_type, current_user
        )
    per_page = 20
    page = request.args.get("page", default=1, type=int)
    search_query = request.args.get("search", default="")
    from_date = request.args.get("from_date", default=None)
    to_date = request.args.get("to_date", default=None)
    list_display = resource_class.list_display
    sort = resource_class.sort if hasattr(resource_class, "sort") else None
    search_params = {
        "search_query": search_query,
        "from_date": from_date,
        "to_date": to_date,
    }
    pagination = filter_resources(
        resource_class=resource_class,
        model=model,
        list_display=list_display,
        search_params=search_params,
        page=page,
        per_page=per_page,
        sort=sort,
    )

    return render_template(
        "resource/list.html",
        pagination=pagination,
        resource_type=resource_type,
        list_display=list_display,
        search_params=search_params,
        hide_search=hide_search,
        hide_date_filter=hide_date_filter,
    )


@admin.route(
    "/resource/<string:resource_type>/create",
    methods=["GET", "POST"],
)
@login_required
def resource_create(resource_type):
    """
    Create a new resource of the specified type.

    This route handler function handles both GET and POST requests
    for creating a new resource of the given type.
    On a GET request, it renders the 'resource/create.html'
    template, which displays a form for entering information
    to create a new resource.
    On a POST request, the function processes the form data,
    validates and converts the attributes, creates a new resource object,
    and adds it to the database.

    Args:
        resource_type (str): The type of resource to create.

    Returns:
        werkzeug.wrappers.response.Response: A redirection response to
        the resource list page after successful creation. On a GET request,
        returns the rendered template with the create form.
        On a POST request, redirects to the resource list page.
    """
    resource_class = get_resource_class(resource_type)
    model = resource_class.model
    editable_attributes = get_editable_attributes(resource_type)
    editable_relations = get_editable_relations(resource_class)
    if request.method == "GET":
        return render_template(
            "resource/create.html",
            resource_type=resource_type,
            editable_attributes=editable_attributes,
            editable_relations=editable_relations,
        )

    attributes_to_save = {}

    for attribute in editable_attributes:
        attribute_value = request.form.get(attribute["name"])
        if attribute["name"] == admin_configs["user"]["secret"]:
            attribute_value = get_hashed_password(attribute_value)

        validated_attribute_value = validate_resource_attribute(
            resource_type, attribute, attribute_value
        )
        attributes_to_save[attribute["name"]] = validated_attribute_value

    new_resource = model(**attributes_to_save)
    db.session.add(new_resource)
    db.session.commit()

    # call after create hook
    if hasattr(resource_class, "after_create_callback"):
        resource_class.after_create_callback(new_resource)

    return redirect(url_for(".resource_list", resource_type=resource_type))


@admin.route(
    "/resource/<string:resource_type>/<string:resource_id>/view",
    methods=["GET"],
)
@login_required
def resource_read(resource_type, resource_id):
    """
    Read an existing resource of the specified type.

    This route handler function displays an entity
    of the given resource type. The resource is retrieved from the
    corresponding model class.

    Args:
        resource_type (str): The type of resource to read.
        resource_id (str): The ID of the resource to read.

    Returns:
        Returns the rendered template with the read page.
    """
    resource_class = get_resource_class(resource_type)
    model = resource_class.model
    resource = model.query.get(resource_id)

    if not resource:
        return redirect(
            request.referrer
            or url_for(".resource_list", resource_type=resource_type)
        )

    readable_attributes = get_readable_attributes(resource_type)

    return render_template(
        "resource/read.html",
        resource_type=resource_type,
        resource=resource,
        readable_attributes=readable_attributes,
        admin_configs=admin_configs,
    )


@admin.route(
    "/resource/<string:resource_type>/<string:resource_id>/edit",
    methods=["GET", "POST"],
)
@login_required
def resource_edit(resource_type, resource_id):
    """
    Edit an existing resource of the specified type.

    This route handler function handles both GET and POST requests for
    editing an existing resource of the given type. On a GET request,
    it renders the 'resource/edit.html' template, which displays a form
    pre-filled with the current attributes of the resource.
    On a POST request, the function processes the form data,
    validates and converts the attributes, updates the resource object,
    and commits the changes to the database.

    Args:
        resource_type (str): The type of resource to edit.
        resource_id (str): The ID of the resource to edit.

    Returns:
        werkzeug.wrappers.response.Response: A redirection response
        to the resource list page after successful editing.
        On a GET request, returns the rendered template with the edit
        form pre-filled.
        On a POST request, redirects to the resource list page.
        If the resource does not exist, redirects to the resource list page.
    """
    resource_class = get_resource_class(resource_type)
    model = resource_class.model
    resource = model.query.get(resource_id)

    if resource_type == "mandi-receipt":
        selected_reasons = request.form.getlist(
            f"rejection_reasons_{resource_id}[]"
        )

        resource.rejection_reason_ids = selected_reasons

    if not resource:
        return redirect(
            request.referrer
            or url_for(".resource_list", resource_type=resource_type)
        )

    editable_attributes = get_editable_attributes(resource_type)
    old_resource = copy.copy(
        resource
    )  # make a clone before there are any updates

    editable_relations = get_editable_relations(resource_class)
    if request.method == "GET":
        return render_template(
            "resource/edit.html",
            resource_type=resource_type,
            resource=resource,
            editable_attributes=editable_attributes,
            admin_configs=admin_configs,
            editable_relations=editable_relations,
        )

    for attribute in editable_attributes:
        if attribute["name"] in request.form:
            attribute_value = request.form.get(attribute["name"])
            if attribute["name"] == admin_configs["user"]["secret"]:
                if attribute_value == "" or attribute_value is None:
                    continue
                attribute_value = get_hashed_password(attribute_value)

            validated_attribute_value = validate_resource_attribute(
                resource_type, attribute, attribute_value
            )
            setattr(resource, attribute["name"], validated_attribute_value)

    if resource_type == "mandi-receipt":
        updated_mandi = MandiModel.query.get(resource.mandi_id)
        updated_crop = CropModel.query.get(resource.crop_id)
        setattr(resource, "mandi_name", updated_mandi.mandi_name)
        setattr(resource, "mandi_name_hi", updated_mandi.mandi_name_hi)
        setattr(resource, "crop_name", updated_crop.crop_name)
        setattr(resource, "crop_name_hi", updated_crop.crop_name_hi)

    if resource_type == "mandi-receipt":
        existing_sale_receipt = SaleReceiptModel.query.filter(
            SaleReceiptModel.id != resource.id,  # Ensure IDs do not match
            SaleReceiptModel.booklet_number == resource.booklet_number,
            SaleReceiptModel.receipt_id == resource.receipt_id,
            SaleReceiptModel.mandi_id == resource.mandi_id,
            SaleReceiptModel.is_approved == True,
            func.date(SaleReceiptModel.receipt_date)
            == func.date(resource.receipt_date),
        ).first()
        if existing_sale_receipt and existing_sale_receipt.id != resource.id:
            duplicate_reason = ReceiptRejectionReason.query.filter(
                ReceiptRejectionReason.short_description
                == "डुप्लीकेट रसीद एंट्री"
            ).first()
            if duplicate_reason is not None:
                if not resource.rejection_reason_ids:
                    resource.rejection_reason_ids = []
                resource.rejection_reason_ids.append(duplicate_reason.id)
            resource.is_approved = False
            resource.token_amount = 0

        sale_receipt = SaleReceiptModel.query.filter(
            SaleReceiptModel.id == resource_id
        ).first()
        action = ""
        if resource.is_approved == True:
            action = "approve"

        process_team_member_validation(
            sale_receipt, resource.rejection_reason_ids, action
        )

    db.session.add(resource)
    db.session.commit()

    handle_resource_revision(
        resource_class=resource_class,
        old_resource=old_resource,
        new_resource=resource,
    )

    # call after update hook
    if hasattr(resource_class, "after_update_callback"):
        resource_class.after_update_callback(resource, old_resource)

    return redirect(
        request.referrer
        or url_for(".resource_list", resource_type=resource_type)
    )


@admin.route(
    "/resource/<string:resource_type>/<string:resource_id>/delete",
    methods=["POST"],
)
@login_required
def resource_delete(resource_type, resource_id):
    """
    Delete an existing resource of the specified type.

    This route handler function handles POST requests for deleting an
    existing resource of the given type. It retrieves the resource by
    its ID, deletes it from the database, and commits the changes.
    After deletion, the function redirects to the resource list page.

    Args:
        resource_type (str): The type of resource to delete.
        resource_id (str): The ID of the resource to delete.

    Returns:
        werkzeug.wrappers.response.Response: A redirection response to
        the resource list page after successful deletion.
    """
    resource_class = get_resource_class(resource_type)
    model = resource_class.model
    resource = model.query.get(resource_id)

    cloned_resource = resource
    if resource:
        db.session.delete(resource)
        db.session.commit()

    # call after update hook
    if cloned_resource and hasattr(resource_class, "after_delete_callback"):
        resource_class.after_delete_callback(cloned_resource)

    return redirect(
        request.referrer
        or url_for(".resource_list", resource_type=resource_type)
    )


@admin.route("/resource/<string:resource_type>/download", methods=["GET"])
@login_required
def resource_download(resource_type):
    """
    Download a CSV file containing the data of resources of the specified type.

    This route handler function handles GET requests for downloading a CSV file
    containing the data of resources of the given type. It retrieves all
    resources of the specified type from the database, creates a CSV
    file containing the data, and returns the CSV file as a
    downloadable attachment.

    Args:
        resource_type (str): The type of resource to download.

    Returns:
        werkzeug.wrappers.response.Response: A response containing the CSV
        file as an attachment with the appropriate headers for downloading.
    """

    resource_class = get_resource_class(resource_type)
    model = resource_class.model
    output = io.StringIO()
    writer = csv.writer(output)

    downloadable_attributes = model.__table__.columns.keys()
    search_query = request.args.get("search", default="")
    from_date = request.args.get("from_date", default=None)
    to_date = request.args.get("to_date", default=None)
    list_display = resource_class.list_display
    search_params = {
        "search_query": search_query,
        "from_date": from_date,
        "to_date": to_date,
    }
    pagination = filter_resources(
        resource_class=resource_class,
        model=model,
        list_display=list_display,
        search_params=search_params,
        page=1,
        per_page=None,
        sort=None,
    )
    resources = pagination.items

    writer.writerow(downloadable_attributes)  # csv header
    for resource in resources:
        line = []
        for attribute in downloadable_attributes:
            line.append(getattr(resource, attribute))
        writer.writerow(line)
    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment;filename="
            + admin_label_plural(resource_type)
            + ".csv"
        },
    )


@admin.route(
    "/resource/<string:resource_type>/download-sample",
    methods=["GET"],
)
@login_required
def resource_download_sample(resource_type):
    """
    Download a CSV file template for uploading new resources of the
    specified type.

    This route handler function handles GET requests for downloading
    a CSV file template for uploading new resources of the given type.
    It generates a CSV file header row with the attributes that can
    be uploaded for the specified resource type and returns the
    CSV file template as a downloadable attachment.

    Args:
        resource_type (str): The type of resource for which to download
        the CSV template.

    Returns:
        werkzeug.wrappers.response.Response: A response containing the CSV
        file template as an attachment with the appropriate headers for
        downloading.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    uploadable_attributes = get_editable_attributes(resource_type)
    col_names = [attribute["name"] for attribute in uploadable_attributes]
    writer.writerow(col_names)  # csv header
    writer.writerow([])  # print a blank second row

    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={
            "Content-Disposition": "attachment;filename="
            + admin_label_plural(resource_type)
            + "-sample.csv"
        },
    )


@admin.route(
    "/resource/<string:resource_type>/upload",
    methods=["GET", "POST"],
)
@login_required
def resource_upload(resource_type):
    """
    Uploads a CSV file containing data for the specified resource type.

    Args:
        resource_type (str): The type of resource being uploaded.

    Returns:
        If a POST request is made with a CSV file, the function processes
        the uploaded data and adds new resource instances to the database.
        Redirects to the resource list page after processing.
        If a GET request is made, renders the upload form.
    """
    resource_class = get_resource_class(resource_type)
    model = resource_class.model

    uploadable_attributes = get_editable_attributes(resource_type)

    if request.method == "POST":
        uploaded_file = request.files["file"]
        col_names = [attribute["name"] for attribute in uploadable_attributes]
        csv_data = pd.read_csv(uploaded_file, usecols=col_names)
        for index, row in csv_data.iterrows():
            attributes_to_save = {}
            for attribute in uploadable_attributes:
                attribute_value = row[attribute["name"]]
                if pd.isna(attribute_value):
                    attribute_value = None
                if (attribute["type"]) in ["VARCHAR", "TEXT", "JSON"]:
                    attribute_value = attribute_value or None
                elif attribute["type"] == "INTEGER":
                    attribute_value = attribute_value or None
                elif (
                    attribute["type"] == "DATE"
                    or attribute["type"] == "DATETIME"
                ):
                    attribute_value = attribute_value or None
                elif attribute["type"] == "BOOLEAN":
                    if not isinstance(attribute_value, bool):
                        attribute_value = (
                            True
                            if attribute_value.lower() == "true"
                            else False
                        )
                    else:
                        attribute_value = bool(attribute_value)
                attributes_to_save[attribute["name"]] = attribute_value
            new_resource = model(**attributes_to_save)
            db.session.add(new_resource)
            db.session.commit()

        if uploaded_file:
            uploaded_file.filename = secure_filename(uploaded_file.filename)
            upload_file_to_s3(uploaded_file)

        flash("All " + resource_type.capitalize() + " uploaded!")
        return redirect(url_for(".resource_list", resource_type=resource_type))
    return render_template("resource/upload.html", resource_type=resource_type)


def get_hashed_password(password):
    """
    Hashes a password using Bcrypt.

    Parameters:
    - password (str): The password to be hashed.

    Returns:
    str: The hashed password encoded as a UTF-8 string.

    Raises:
    None

    Example:
    hashed_password = get_hashed_password('my_secure_password')
    """
    if password == "" or password is None:
        return password

    bcrypt.init_app(app)
    return bcrypt.generate_password_hash(password, 10).decode("utf-8")


def get_preprocess_data(pagination, list_display):
    processed_data = []

    for resource in pagination.items:
        image_data = []
        button_data = []
        other_data = []
        receipt_date = getattr(resource, "receipt_date")
        formatted_receipt_date = receipt_date.strftime("%Y-%m-%d")
        formatted_time = receipt_date.strftime("%I:%M %p")

        for item in list_display:
            if item == "receipt_image_url":
                image_data.append(getattr(resource, item))
            elif item == "is_approved":
                button_data.extend(
                    [
                        ("Edit", resource.id),
                        ("Approve", resource.id),
                        ("Reject", resource.id),
                    ]
                )
            elif item != "receipt_date":
                other_data.append((item, getattr(resource, item)))

        other_data.append(("receipt_date", formatted_receipt_date))
        other_data.append(("Time", formatted_time))

        processed_data.append((image_data, button_data, other_data))

    return processed_data


def get_editable_relations(resource_class):
    editable_relations = {}
    if hasattr(resource_class, "editable_relations_dropdown"):
        for editable_relation in resource_class.editable_relations_dropdown:
            attribute_key = editable_relation["key"]
            related_model = editable_relation["related_model"]
            related_label = editable_relation["related_label"]
            related_key = editable_relation["related_key"]
            related_data = related_model.query.order_by(related_label).all()
            editable_relations[attribute_key] = {}
            editable_relations[attribute_key]["label"] = editable_relation[
                "label"
            ]
            editable_relations[attribute_key]["options"] = [
                {
                    "label": getattr(data, related_label),
                    "value": getattr(data, related_key),
                }
                for data in related_data
            ]
    return editable_relations


@admin.route("/update_approval_status", methods=["POST"])
def update_receipt_status():
    response = update_approval_status(current_user)

    return response


@admin.route("/update-receipt-data", methods=["POST"])
def update_receipt_data():
    form_data = request.json
    response = update_extracted_receipt_data(form_data)

    return response


def handle_resource_revision(resource_class, old_resource, new_resource):
    if (
        hasattr(resource_class, "revisions")
        and hasattr(resource_class, "revision_model")
        and resource_class.revisions
    ):
        revision_model = resource_class.revision_model
        revision_pk = resource_class.revision_pk

        # check if resource has been edited then only create the revision
        is_modified = False
        for column, value in old_resource.__dict__.items():
            if column in [
                "id",
                "_sa_instance_state",
                "created_at",
                "updated_at",
            ]:
                continue

            if getattr(old_resource, column) != getattr(new_resource, column):
                is_modified = True
                break

        if not is_modified:
            return

        cloned_attributes_to_save = {}
        for column, value in old_resource.__dict__.items():
            if column in [
                "_sa_instance_state",
                "created_at",
                "updated_at",
            ]:
                continue

            if column == "id":
                cloned_attributes_to_save[revision_pk] = value
                continue

            cloned_attributes_to_save[column] = value

        if current_user and "edited_by" in revision_model.__table__.columns:
            cloned_attributes_to_save["edited_by"] = current_user.id

        cloned_resource = revision_model(**cloned_attributes_to_save)
        db.session.add(cloned_resource)
        db.session.commit()
