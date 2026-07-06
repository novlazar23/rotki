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


class CCXTProfileRunSchema(Schema):
    name = fields.String(required=True)
    start_ms = fields.Integer(required=True)
    end_ms = fields.Integer(required=False, load_default=None, allow_none=True)
    limit = fields.Integer(required=False, load_default=200)
    max_pages = fields.Integer(required=False, load_default=10)


class CCXTProfileDeleteSchema(Schema):
    name = fields.String(required=True)


class CCXTProfilesResource(BaseMethodView):
    put_schema = CCXTProfileSchema()
    patch_schema = CCXTProfileSchema()
    post_schema = CCXTProfileRunSchema()
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
    @use_kwargs(post_schema, location='json')
    def post(
            self,
            name: str,
            start_ms: int,
            end_ms: int | None,
            limit: int,
            max_pages: int,
    ) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).preview_stored_profile_history(
            name=name,
            start_ms=start_ms,
            end_ms=end_ms,
            limit=limit,
            max_pages=max_pages,
        )
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))

    @require_loggedin_user()
    @use_kwargs(delete_schema, location='json')
    def delete(self, name: str) -> Response:
        result = CCXTService(self.rest_api.rotkehlchen).delete_profile(name=name)
        return api_response(result, status_code=result.get('status_code', HTTPStatus.OK))
