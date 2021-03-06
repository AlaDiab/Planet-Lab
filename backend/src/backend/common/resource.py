"""Common tools for building restful resources."""


import flask
import flask_restful
import flask_restful.reqparse
import functools
import sqlalchemy
import sqlalchemy.orm as orm

import backend
import backend.common.auth as auth


class RequestParser(flask_restful.reqparse.RequestParser):
    """RequestParser subclass which correctly handles nulls in
    non-required fields.
    """
    def add_argument(self, *args, **kwargs):
        """Replace the 'type' function in-place to handle null values
        correctly depending on if the argument is required.
        If the argument is required, a null value should result in a
        400 error.  Otherwise, a null value is acceptable input.
        In the latter case, we avoid errors which come from calling the
        'type' function on null values.
        """
        is_required = kwargs.get('required', False)
        type_func = kwargs.get('type')

        if type_func is not None:
            if is_required:
                @functools.wraps(type_func)
                def new_type_func(arg):
                    """Raise an error on None values."""
                    if arg is None:
                        raise ValueError("Required value may not be null")
                    else:
                        return type_func(arg)
            else:
                @functools.wraps(type_func)
                def new_type_func(arg):
                    """Don't call type_func on None values."""
                    if arg is None:
                        return arg
                    else:
                        return type_func(arg)

            kwargs['type'] = new_type_func

        return super(RequestParser, self).add_argument(*args, **kwargs)


class SimpleResource(flask_restful.Resource):
    """Base class defining the simplest common set of CRUD endpoints
    for working with single resources.
    """
    # Child classes need to define a reqparse.RequestParser instance
    # for the parser attribute to be used when parsing PUT requests.
    parser = None

    @staticmethod
    def query(*args, **kwargs):
        """Needs to be implemented by child classes.  Should return
        a query to select the row being operated upon by the GET,
        PUT and DELETE verbs.
        """
        raise NotImplementedError

    def as_dict(self, resource):
        """Needs to be implemented by child classes.  Given an object,
        returns a serializable dictionary representing that object to
        be returned on GET's.
        """
        raise NotImplementedError

    def get(self, *args, **kwargs):
        """Return a serialization of the resource or a 404."""
        resource = self.query(*args, **kwargs).first()
        if resource is None:
            return flask.Response('', 404)
        else:
            return self.as_dict(resource)

    def put(self, *args, **kwargs):
        """Update a resource."""
        resource = self.query(*args, **kwargs).first()
        if resource is None:
            return flask.Response('', 404)
        else:
            update = self.parser.parse_args()
            for key, value in update.iteritems():
                setattr(resource, key, value)
            backend.db.session.commit()
            return self.as_dict(resource)

    def delete(self, *args, **kwargs):
        """Delete a resource."""
        rows_deleted = self.query(*args, **kwargs).delete(
                synchronize_session=False)
        backend.db.session.commit()

        if not rows_deleted:
            return flask.Response('', 404)


class SimpleCreate(flask_restful.Resource):
    """Base class defining the simplest POST for a new resource."""

    parser = None
    resource_type = None

    def as_dict(self, resource):
        """Needs to be implemented by child classes.  Given an object,
        returns a serializable dictionary representing that object to
        be returned after the resource is created.
        """
        raise NotImplementedError

    def post(self):
        """Create a new resource and link it to its creator."""
        args = self.parser.parse_args()
        args['creator_id'] = auth.current_user_id()
        new_resource = self.resource_type(**args) #pylint: disable=E1102

        backend.db.session.add(new_resource)
        backend.db.session.commit()

        return self.as_dict(new_resource)

class ManyToOneLink(flask_restful.Resource):
    """Resource dealing with creating and listing a resource linked
    to one single other resource.
    """
    # child classes need to override all of these
    parent_id_name = None
    child_link_name = None
    resource_type = None
    parent_resource_type = None
    parser = None


    def as_dict(self, resource):
        """Needs to be implemented by child classes.  Given an object,
        returns a serializable dictionary representing that object to
        be returned on GET's.
        """
        raise NotImplementedError

    def post(self, parent_id):
        """Create a new resource and link it to its creator and parent."""
        args = self.build_args(parent_id)
        return self.create_resource(args)

    def build_args(self, parent_id):
        """Build a dictionary from the parsed request arguments
        representing the new resource to be created.
        """
        args = self.parser.parse_args()
        args['creator_id'] = auth.current_user_id()
        args[self.parent_id_name] = parent_id
        return args

    def create_resource(self, args):
        """Given a dictionary representing the new resource to be
        created, insert that new resource into the database and
        return a representation of the created resource.
        """
        new_resource = self.resource_type(**args) #pylint: disable=E1102
        try:
            backend.db.session.add(new_resource)
            backend.db.session.commit()
        except sqlalchemy.exc.IntegrityError:
            # tried to link to a non-existent parent
            return flask.Response('', 404)
        else:
            return self.as_dict(new_resource)

    def get(self, parent_id):
        """Return children linked to a given parent."""
        parent = self.parent_resource_type.query.filter_by(
                id=parent_id).options(
                        orm.joinedload(self.child_link_name)).first()
        if parent is None:
            return flask.Response('', 404)
        else:
            return {self.child_link_name: [
                self.as_dict(child) for child in
                getattr(parent, self.child_link_name)]}


class ManyToManyLink(flask_restful.Resource):
    """Resource dealing with many-to-many links between collections."""

    left_id_name = None
    right_id_name = None
    join_table = None

    def put(self, left_id, right_id):
        """Create a link between the two given ids in the join table."""

        insert = self.join_table.insert().values({
            self.left_id_name: left_id,
            self.right_id_name: right_id})
        try:
            backend.db.session.execute(insert)
            backend.db.session.commit()
        except sqlalchemy.exc.IntegrityError:
            # We hit a unique constraint for this combination
            # of ids, so we happily let the insert fail.
            pass

    def delete(self, left_id, right_id):
        """Delete a link between the two given ids in the join table."""
        delete = self.join_table.delete().where(sqlalchemy.and_(
            self.left_id_name == left_id, self.right_id_name == right_id))

        res = backend.db.session.execute(delete)
        backend.db.session.commit()

        if not res.rowcount:
            return flask.Response('', 404)
