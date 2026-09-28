# Rate limiting

Source: https://support.happyfox.com/kb/article/1148-rate-limiting/ (last updated 2022-03-14 12:27:24 UTC)

HappyFox employs a number of safeguards against bursts of incoming traffic to help maximise its stability.

One among them is a global rate limiter for the Helpdesk Developer API requests. Users who send many requests in quick succession may see error responses that show up as HTTPS status code `429`.

HappyFox allows up to

- 500 GET requests / minute
- 300 POST requests / minute

This is applicable for all public Developer API endpoints published in our documentation.

When the above threshold is exceeded, client would receive a “429” error response for the next 10 mins.

Please note - above limits take precedence over the limits defined under each API key.
