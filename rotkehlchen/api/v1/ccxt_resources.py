from __future__ import annotations

from http import HTTPStatus
from typing import Any

from flask import Response
from marshmallow import Schema, fields
from webargs.flaskparser import use_kwargs

from rotkehlchen.api.rest import api_response
from rotkehlchen.api.services.ccxt import CCXTService
from rotkehlchen.api.v1.common_resources import BaseMethodView
from rotkehlchen.api.v1.resources import require_loggedin_user


class CCXTProfileSchema(Schema):
    profile = fields.Dict(required=True)


class CCXTProfileDeleteSchema(Schema):
    name = fields.String(required=True)


class CCXTProfilesResource(BaseMethodView):
    put_schema = CCXTProfileSchema()
    patch_schema = CCXTProfileSchema()
    delete_schema = CCXTProfileDeleteSchema()

    @require_loggedin_user()
    def get(self) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).get_profiles()
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))

    @require_loggedin_user()
    @use_kwargs(put_schema, location='json')
    def put(self, profile: dict[str, Any]) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).upsert_profile(profile=profile)
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))

    @require_loggedin_user()
    @use_kwargs(patch_schema, location='json')
    def patch(self, profile: dict[str, Any]) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).upsert_profile(profile=profile)
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))

    @require_loggedin_user()
    @use_kwargs(delete_schema, location='json')
    def delete(self, name: str) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).delete_profile(name=name)
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))
