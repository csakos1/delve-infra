KEY=$(grep '^API_SERVER_KEY=' ~/.hermes/.env | cut -d= -f2-)
H=(-H "Authorization: Bearer $KEY" -H "Content-Type: application/json")
B=http://127.0.0.1:8642

# start <név> <prompt> [session_id]  → RID, háttérben menti az SSE-t <név>.events.sse-be
start() {
  local body; body=$(jq -nc --arg i "$2" --arg s "${3:-}" '{input:$i} + (if $s!="" then {session_id:$s} else {} end)')
  RID=$(curl -s "${H[@]}" -X POST $B/v1/runs -d "$body" | tee $1.create.json | jq -r .run_id)
  echo "run_id=$RID"
  curl -sN "${H[@]}" $B/v1/runs/$RID/events > $1.events.sse &
}
# status [fájlnév]  → GET /v1/runs/$RID
status() { curl -s "${H[@]}" $B/v1/runs/$RID | tee ${1:-status}.json | jq .; }
# answer <once|deny> <events-fájl>
answer() {
  local r; r=$(grep '"approval.request"' "$2" | tail -1 | sed 's/^data: //' | jq -r .request_id)
  curl -s "${H[@]}" -X POST $B/v1/runs/$RID/approval \
    -d "$(jq -nc --arg r "$r" --arg c "$1" '{request_id:$r, choice:$c}')" | tee approval-$1-$RID.json; echo
}
# ev <events-fájl>  → a fontos események tömören
ev() { grep '^data:' "$1" | sed 's/^data: //' | jq -c 'select(.event|test("tool|approval|run\\.|interim"))'; }
