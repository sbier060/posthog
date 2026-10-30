from typing import cast

from posthog.schema import AccountUsersQuery, ActorsQuery, HogQLQueryResponse

from posthog.hogql import ast
from posthog.hogql.context import HogQLContext
from posthog.hogql.database.models import LazyTableToAdd
from posthog.hogql.database.schema.persons import PersonsTable
from posthog.hogql.errors import QueryError
from posthog.hogql.parser import parse_expr
from posthog.hogql.visitor import clone_expr

from posthog.hogql_queries.actor_strategies import ActorStrategy, PersonStrategy
from posthog.hogql_queries.paginators import HogQLHasMorePaginator
from posthog.hogql_queries.query_runner import AnalyticsQueryRunner
from posthog.models import User
from posthog.schema_enums import PersonsArgMaxVersion

from products.access_control.backend.facade.api import (
    get_restricted_properties_with_group_type_index_for_team,
    split_restricted_property_names,
)
from products.access_control.backend.facade.user_access_control import UserAccessControlError
from products.customer_analytics.backend.constants import (
    ACCOUNT_USERS_QUERY_ACCESS_LABEL_PREFIX,
    ACCOUNT_USERS_QUERY_STATUS_LABEL_PREFIX,
)
from products.customer_analytics.backend.logic.account_persons import AccountPersonsReader


class AccountUsersStrategy(PersonStrategy):
    def filter_conditions(self) -> list[ast.Expr]:
        conditions = PersonStrategy(
            team=self.team,
            user=self.user,
            paginator=self.paginator,
            query=self.query.model_copy(update={"search": None}),
        ).filter_conditions()
        if self.query.search and self.query.search.strip():
            source = self.query.source
            assert isinstance(source, AccountUsersQuery)
            reader = AccountPersonsReader.create(team=self.team, user=self.user, account_id=source.accountId)
            if reader is None:
                raise UserAccessControlError("account", "viewer")
            restrictions = get_restricted_properties_with_group_type_index_for_team(
                user=self.user, team_id=self.team.pk
            )
            condition = reader.get_search_filter(
                self.query.search.strip(), split_restricted_property_names(restrictions).person
            )
            conditions.append(parse_expr(condition.key))
        return conditions


class AccountUsersPersonsTable(PersonsTable):
    def lazy_select(
        self, table_to_add: LazyTableToAdd, context: HogQLContext, node: ast.SelectQuery
    ) -> ast.SelectQuery:
        query = cast(ast.SelectQuery, super().lazy_select(table_to_add, context, node))
        if self.filter is not None and query.where is not None:
            query.prewhere = ast.And(exprs=[clone_expr(self.filter, True), query.where])
            query.where = None
        return query


class AccountUsersQueryRunner(AnalyticsQueryRunner[HogQLQueryResponse]):
    query: AccountUsersQuery

    def get_reader(self) -> AccountPersonsReader:
        reader = AccountPersonsReader.create(team=self.team, user=self.user, account_id=self.query.accountId)
        if reader is None:
            raise UserAccessControlError("account", "viewer")
        return reader

    def validate_query_runner_access(self, user: User) -> bool:
        if AccountPersonsReader.create(team=self.team, user=user, account_id=self.query.accountId) is None:
            raise UserAccessControlError("account", "viewer")
        return True

    def get_cache_key_variant(self) -> str:
        try:
            reader = self.get_reader()
        except UserAccessControlError:
            return "denied"
        return reader.get_access_key()

    def query_status_labels(self) -> list[str]:
        return [
            f"{ACCOUNT_USERS_QUERY_STATUS_LABEL_PREFIX}{self.query.accountId}",
            f"{ACCOUNT_USERS_QUERY_ACCESS_LABEL_PREFIX}{self.get_reader().get_access_key()}",
        ]

    def to_query(self) -> ast.SelectQuery:
        self.modifiers.personsArgMaxVersion = PersonsArgMaxVersion.V2
        return self.get_reader().get_source_query()

    def to_actors_query(self) -> ast.SelectQuery:
        return self.to_query()

    def _calculate(self) -> HogQLQueryResponse:
        raise QueryError("Use AccountUsersQuery as an ActorsQuery source.")

    def get_actors_hogql_context(self, context: HogQLContext) -> HogQLContext:
        assert context.database is not None
        root = context.database.tables
        persons = root.get_child(["persons"])
        assert isinstance(persons.table, PersonsTable)
        table = AccountUsersPersonsTable(fields=persons.table.fields, filter=persons.table.filter)
        context.database = context.database.model_copy(
            update={
                "tables": root.model_copy(
                    update={"children": {**root.children, "persons": persons.model_copy(update={"table": table})}}
                )
            }
        )
        return context

    def get_actors_strategy(self, query: ActorsQuery, paginator: HogQLHasMorePaginator) -> ActorStrategy:
        return AccountUsersStrategy(team=self.team, query=query, paginator=paginator, user=self.user)

    def validate_actors_query(self, query: ActorsQuery) -> None:
        if not query.select or any(column.strip() in {"person", "actor"} for column in query.select):
            raise QueryError("Select explicit person columns for account users.")

    def get_person_display_name_column(self) -> ast.Expr:
        reader = self.get_reader()
        restrictions = get_restricted_properties_with_group_type_index_for_team(user=self.user, team_id=self.team.pk)
        return ast.Tuple(
            exprs=[
                reader.get_display_name(split_restricted_property_names(restrictions).person),
                ast.Call(name="toString", args=[ast.Field(chain=["persons", "id"])]),
            ]
        )
