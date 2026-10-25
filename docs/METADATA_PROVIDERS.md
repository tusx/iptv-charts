# Metadata provider authentication

This page records the exact provider contracts the app uses and why.

## TMDB

Official documentation:

- Authentication: <https://developer.themoviedb.org/docs/authentication-application>
- API reference: <https://developer.themoviedb.org/reference/intro/getting-started>
- Token and key settings: <https://www.themoviedb.org/settings/api>

TMDB's current application authentication uses the **API Read Access Token**
from the account's API settings. It is a JWT (it starts with `eyJ`) and is sent
on every request as:

```http
Authorization: Bearer <API_READ_ACCESS_TOKEN>
```

The endpoints remain under `https://api.themoviedb.org/3`. The older
32-character v3 API key is a different credential and would be sent as an
`api_key` query parameter; this app no longer uses it. Saving a 32-hex v3 key
is rejected with a message pointing at the read token instead.

Implementation:

- `iptv_charts/clients/tmdb.py` validates the token (`validate_read_token`)
  and always sets the `Authorization` header.
- `TMDB_KEY = "tmdb_read_token"` is the current setting. A JWT saved under the
  pre-0.5 key `tmdb_api_key` is still read for compatibility; saving a new
  token or clearing the credential clears the old key too.
- Requests use `language` as a query parameter but never a credential.
- `GET /search/movie`, `GET /search/tv`, `GET /movie/{id}`,
  `GET /tv/{id}`, and `GET /tv/{id}/season/{n}` are used.
- Movie/TV details request `append_to_response=external_ids,alternative_titles`.
  The returned TMDB and IMDb ids are stored on movie rows; the IMDb id is used
  for ID-first catalog matching when a provider supplies it.
- **Explore discovery feeds** use these read-only list endpoints:
  - movies: `/trending/movie/{day|week}`, `/movie/popular`,
    `/movie/top_rated`, `/movie/upcoming`, `/movie/now_playing`;
  - series: `/trending/tv/{day|week}`, `/tv/popular`, `/tv/top_rated`,
    `/tv/on_the_air`, `/tv/airing_today`.

  The Explore tabs show trending, popular, top rated and upcoming for movies,
  and those plus on the air and airing today for series. Weekly trending is the
  default. Feeds are cached in `metadata_cache` for 15 minutes and normalised
  into the same summary shape as search results, with an extra `media_type`
  field. The Explore API never returns a provider URL or credential.
- Posters are served through the local image proxy (`/api/image/<size>/<path>`),
  which whitelists TMDB sizes and validates the path. No page loads images from
  a CDN.
- `429 Retry-After` is honoured once; `401/403` is reported as a token error;
  `404` is reported without leaking the request URL.

## TheTVDB v4

Official documentation:

- Swagger UI: <https://thetvdb.github.io/v4-api/>
- OpenAPI spec: <https://thetvdb.github.io/v4-api/swagger.yml>
- Repository: <https://github.com/thetvdb/v4-api>
- Official Python client:
  <https://github.com/thetvdb/tvdb-v4-python>

Base URL: `https://api4.thetvdb.com/v4`.

### Login

```http
POST /login
Content-Type: application/json

{"apikey": "<key>", "pin": "<subscriber pin, optional>"}
```

The response is `{"status": "...", "data": {"token": "<JWT>"}}`. The token is
valid for about one month and is sent as `Authorization: Bearer <token>`.
The app caches the token in `metadata_cache` and re-logins once on `401`; the
cached token is cleared when the key or PIN changes.

### Search

`GET /search` with `query`, `type=series`, `limit`, and `year` when the library
row has one. Results are in `data`; the app reads `tvdb_id`, `name`, `year`,
`overview`, `image_url`, and `first_air_time`. The search result cap/overflow
error is not treated as a rate limit.

### Episodes

The app uses the translated season-order endpoint:

```http
GET /series/{id}/episodes/default/{lang}?page=0
```

- `default` is TVDB's default season order (other season orders exist:
  `official`, `dvd`, `absolute`, `alternate`, `regional`).
- `lang` is an ISO 639-2/B code; the app uses `eng`.
- `page` starts at 0 and is required by the spec.
- The response contains `data.series` and `data.episodes` (the official client
  returns `series`/`episodes`), with top-level `links` for pagination.
- `links.next` is a page number (the app also tolerates a string or a URL
  containing `page=N`). The app follows it until absent, deduplicates
  `(season, episode)`, and caps at 100 pages.
- The translated endpoint's OpenAPI schema is incomplete and does not list an
  episodes array. If the first translated page has no episodes, the client
  retries the untranslated
  `GET /series/{id}/episodes/default?page=0`, whose schema explicitly includes
  `data.episodes`, and continues with that form.

Episode normalisation reads `seasonNumber`, `number`, `absoluteNumber`, `name`,
`overview`, `aired`, `id`, `image`, and `runtime`. Specials are stored as season
0 but are never auto-monitored.

### TMDB -> TVDB identity

The primary link is `GET /tv/{tmdb_id}?append_to_response=external_ids` and its
`external_ids.tvdb_id`. If missing, the app searches TVDB by title/year and
scores the result against the library title and aliases. TVDB's exact
`/search/remoteid/{remoteId}` endpoint is not used because the series library
does not store an IMDb id; title/year search is the documented fallback.

### Failure behaviour

- Missing TVDB key: TMDB seasons are used.
- TVDB login/search/episode errors: logged, then TMDB seasons are used.
- A TVDB error never prevents a series from being added; it only changes the
  episode metadata source.
