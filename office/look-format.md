# The building's look: company/look.json

The live office at http://office:8771 draws the company's building. Its look is yours: write
`/workspace/company/look.json` and the office applies it within a second, for you and for the person
watching. It is data, not code. Everything below is optional, and anything outside this list, or
malformed, is left out; http://office:8771/state.json lists what was left out under `look.problems`.

```json
{
  "sign": "ACME",
  "motto": "Small worlds, built daily",
  "colors": {"sky": "#23314F", "tower": "#3B3552", "lobby": "#D9C9A8", "head": "#E3D2B0"},
  "logo": {"pixels": ["..##..", ".#oo#.", "#oooo#", ".#oo#.", "..##.."],
           "colors": {"#": "#f5a623", "o": "#2dd4bf"}},
  "floors": {
    "builder": {"name": "The Workshop", "wall": "#EADBC2",
                "decor": ["plant", {"thing": "poster", "text": "Ship it", "color": "#ef476f"}, "clock"]}
  },
  "lobby": {"decor": [{"thing": "trophy", "text": "First sale"}, "aquarium"]},
  "head": {"decor": ["bookshelf", {"thing": "neon", "text": "THINK", "color": "#2dd4bf"}]},
  "merch": [
    {"item": "cap", "color": "#2dd4bf", "for": "everyone"},
    {"item": "shirt", "color": "#ef476f", "for": "builder"},
    {"item": "mug", "color": "#F3EFE6", "for": "head"}
  ]
}
```

- `sign` (up to 24 characters) is the name on the lobby, `motto` (up to 60) the line under it.
- Colors are always `#rrggbb`. `colors` sets the sky, the tower's walls, the lobby and the Head's floor.
- `logo`: up to 16 rows of up to 16 characters. Each character is one pixel, coloured by `colors`
  (up to 8 characters, each a colour); any other character is empty. It shows on the roof and in the lobby.
- `floors` is keyed by role name, as in company/roles/<role>.md. `name` (up to 24 characters) is the
  floor's sign, `wall` its colour, `decor` up to 6 things along its wall.
- A decor thing is one of: plant, tall-plant, lamp, poster, painting, clock, banner, neon, window,
  whiteboard, trophy, bookshelf, aquarium, server-rack, arcade, flag, beanbag. Write it as a name,
  or as an object with `thing`, an optional `text` (up to 20 characters, shown on posters, banners,
  neon signs, whiteboards, trophies and flags) and an optional `color`.
- `merch` (up to 8) is what the people wear: cap, shirt, badge, scarf, headphones or mug, each with
  a `color` and `for`: everyone, head, or a role name.
- The file stays under 64 KB.
