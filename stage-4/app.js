"use strict";
const $ = selector => document.querySelector(selector);
const main = $("#main");
let user;
try { user = JSON.parse(localStorage.getItem("gather-user") || "null"); } catch { user = null; }
if (!user || typeof user.token !== "string") user = null;
let searchEpoch = 0, result = null, selection = null, pending = null, submitting = false;

function node(tag, attrs={}, text="") {
  const el = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key === "class") el.className=value;
    else el.setAttribute(key,value);
  }
  if (text) el.textContent=text;
  return el;
}
function feedback(parent, testid, text, kind="error") {
  clearFeedback(parent, testid);
  const el=node("div", {"data-testid":testid,class:"feedback "+kind,role:kind==="error"?"alert":"status"}, text);
  parent.append(el);
}
function clearFeedback(parent, testid) { parent.querySelectorAll('[data-testid="'+testid+'"]').forEach(el=>el.remove()); }
function message(error) {
  const code=error?.code;
  return ({
    table_unavailable:"That seating option was just taken. We refreshed availability. Choose another table or time and try again.",
    unauthenticated:"Please sign in to continue.",
    email_taken:"That email already has an account. Please sign in.",
    cutoff_passed:"This reservation is inside the restaurant’s cancellation window and cannot be cancelled.",
    not_found:"We couldn’t find that reservation for your account. Check the reference and try again.",
    party_exceeds_capacity:"Your party is larger than this seating option. Please choose another table or pair.",
    validation_failed:"Please check the details and try again.",
    reservation_cancelled:"This reservation has already been cancelled.",
    not_on_slot_grid:"Please choose a time from the availability list.",
    outside_opening_hours:"That time is outside the restaurant’s bookable hours.",
    invalid_local_time:"That local time is unavailable because the clocks change.",
    combination_not_allowed:"These tables cannot be combined."
  })[code] || "We couldn’t complete this request. Please try again.";
}
async function api(path, options={}) {
  const response=await fetch(path, {method:options.method||"GET",
    headers:{"Content-Type":"application/json",...(user?{Authorization:"Bearer "+user.token}:{}),...(options.key?{"Idempotency-Key":options.key}:{})},
    ...(options.body!==undefined?{body:JSON.stringify(options.body)}:{})});
  const body=response.status===204?null:await response.json();
  if (!response.ok) { const error=new Error(message(body?.error)); error.confirmed=true; error.code=body?.error?.code; throw error; }
  return body;
}
function account() {
  const area=$("#account"); area.replaceChildren();
  if (user) {
    area.append(node("span",{"data-testid":"current-user",class:"user"},user.display_name));
    const logout=node("button",{"data-testid":"logout-button",class:"text-button",type:"button"},"Sign out");
    logout.onclick=()=>{localStorage.removeItem("gather-user"); user=null; window.location.href="/";};
    area.append(logout);
  } else {
    area.append(node("a",{href:"/login"},"Sign in"),node("a",{href:"/signup",class:"signup-link"},"Join Gather"));
  }
  document.querySelectorAll("nav > a").forEach(a=>{
    if (a.getAttribute("href")===location.pathname) a.setAttribute("aria-current","page");
  });
}
function field(parent, label, testid, type, value="") {
  const group=node("div",{class:"field"});
  const id=testid;
  group.append(node("label",{for:id},label));
  const input=node("input",{id,"data-testid":testid,type, ...(type==="number"?{min:"1",step:"1"}:{})});
  input.value=value;
  group.append(input); parent.append(group);
  return input;
}
function authScreen(signup) {
  main.innerHTML='<section class="auth-wrap"><p class="eyebrow">Your next lovely evening</p><h1>'+ (signup?"Take your seat.":"Welcome back.") +'</h1><p class="subtle">'+(signup?"Make room for good food and better company.":"Sign in to book a table or manage your reservation.")+'</p><form class="form-card" id="auth-form" novalidate></form></section>';
  const form=$("#auth-form"), prefix=signup?"signup":"login";
  const email=field(form,"Email address",prefix+"-email","email");
  email.autocomplete="email";
  const password=field(form,"Password",prefix+"-password","password");
  password.autocomplete=signup?"new-password":"current-password";
  let display;
  if(signup) { display=field(form,"Your name","signup-display-name","text"); display.autocomplete="name"; }
  const submit=node("button",{"data-testid":prefix+"-submit",type:"submit",class:"primary"},signup?"Create account":"Sign in");
  form.append(submit);
  const bottom=node("p",{class:"form-bottom"},signup?"Already joining us? ":"New to Gather? ");
  bottom.append(node("a",{href:signup?"/login":"/signup"},signup?"Sign in":"Create an account")); form.append(bottom);
  form.onsubmit=async e=>{
    e.preventDefault(); clearFeedback(form,"auth-error"); submit.disabled=true;
    try {
      user=await api("/auth/"+prefix,{method:"POST",body:{email:email.value,password:password.value,...(signup?{display_name:display.value}:{})}});
      localStorage.setItem("gather-user",JSON.stringify(user)); account(); window.location.href="/";
    } catch(error) { feedback(form,"auth-error",error.confirmed?error.message:"We couldn’t connect. Please try again."); }
    finally { submit.disabled=false; }
  };
}
function readableDate(date) {
  const day=new Date(date+"T12:00:00");
  return Number.isNaN(day.getTime())?date:day.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric",year:"numeric"});
}
function tableLabels(restaurant, ids) {
  return ids.map(id=>restaurant?.tables?.find(t=>t.id===id)?.label || id);
}
function seatingText(restaurant, ids) {
  const labels=tableLabels(restaurant,ids);
  return labels.length===1?"Table "+labels[0]:"Tables "+labels.join(" & ");
}
function identity() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16))).map(x=>x.toString(16).padStart(2,"0")).join("");
}
function placeholder() {
  $("#booking-area").innerHTML='<aside class="booking-placeholder"><div class="round-icon" aria-hidden="true">↗</div><h3>A place for your plans</h3><p class="subtle">Choose an available table and time. We’ll save a seat for everyone.</p><p class="subtle">Combined tables bring larger parties together.</p></aside>';
}
function searchScreen() {
  main.innerHTML='<section class="hero"><div><p class="eyebrow">Make an evening of it</p><h1>Good food.<br>Great company.</h1><p>Find your restaurant, pick a time, and make room for the people who matter.</p></div><aside class="hero-note"><strong>One table. Many stories.</strong>Book a cosy spot or bring everyone together at a combined table.</aside></section><form class="search-panel" id="search-form" novalidate><div class="field"><label for="restaurant-select">Where shall we meet?</label><select id="restaurant-select" data-testid="restaurant-select"><option value="">Loading restaurants…</option></select></div></form><div class="content-layout"><section><div class="results-heading"><h2>Find your moment</h2><span id="result-caption" class="subtle">Times are local to the restaurant</span></div><div id="results"><div class="empty-state"><h3>An evening worth planning</h3>Select a restaurant, date and party size to see available tables.</div></div></section><div id="booking-area"></div></div>';
  const form=$("#search-form");
  const date=field(form,"When?","date-input","date",new Date().toLocaleDateString("en-CA"));
  // Build local date explicitly; locale implementations may use a different display format.
  const today=new Date(); date.value=today.getFullYear()+"-"+String(today.getMonth()+1).padStart(2,"0")+"-"+String(today.getDate()).padStart(2,"0");
  field(form,"Your party","party-size-input","number","2");
  form.append(node("button",{"data-testid":"search-button",class:"primary",type:"submit"},"Find a table"));
  placeholder();
  api("/restaurants").then(data=>{
    const select=$("#restaurant-select"); select.replaceChildren();
    if(!data.restaurants.length) { select.append(node("option",{value:""},"No restaurants yet")); return; }
    data.restaurants.forEach(r=>select.append(node("option",{value:r.id},r.name)));
  }).catch(()=>feedback(form,"search-error","Restaurants are unavailable right now. Refresh to try again."));
  form.onsubmit=e=>{
    e.preventDefault();
    const q={restaurant_id:$("#restaurant-select").value,date:$("#date-input").value,party_size:$("#party-size-input").value};
    selection=null; pending=null; placeholder();
    loadSearch(q);
  };
}
async function loadSearch(q, preserve=false) {
  const epoch=++searchEpoch;
  clearFeedback($("#search-form"),"auth-error"); clearFeedback($("#search-form"),"search-error");
  $("#results").innerHTML='<div class="loading" role="status"><span class="spinner"></span>Finding your table…</div>';
  try {
    const [restaurant,availability]=await Promise.all([
      api("/restaurants/"+encodeURIComponent(q.restaurant_id)),
      api("/availability?"+new URLSearchParams(q))]);
    if(epoch!==searchEpoch) return;
    result={q,restaurant,availability}; renderGrid();
    $("#result-caption").textContent=restaurant.name+" · "+readableDate(q.date)+" · "+q.party_size+" guests · "+restaurant.timezone;
  } catch(error) {
    if(epoch!==searchEpoch) return;
    $("#results").replaceChildren();
    feedback($("#results"),"search-error",error.code==="not_found"?"That restaurant is no longer available. Choose another one.":error.confirmed?error.message:"We couldn’t load availability. Please try again.");
  }
}
function renderGrid() {
  const container=$("#results"); container.replaceChildren();
  if(!result.availability.slots.length) {
    container.append(node("div",{"data-testid":"no-slots",class:"empty-state"},"No bookable times on this date. Try another day.")); return;
  }
  const grid=node("div",{"data-testid":"availability-grid",class:"results-grid"});
  for (const slot of result.availability.slots) {
    const time=slot.starts_at_local.slice(11,16), card=node("article",{class:"slot-card"});
    const heading=node("div",{class:"slot-time"},time); heading.append(node("span",{},"Local time")); card.append(heading);
    const choices=node("div",{class:"choices"});
    for (const table of result.restaurant.tables) {
      addSeat(choices,slot,[table.id],table.capacity,slot.available_table_ids.includes(table.id),false);
    }
    for (const option of slot.available_options||[]) {
      if(option.table_ids.length===2) addSeat(choices,slot,option.table_ids,option.capacity,true,true);
    }
    card.append(choices); grid.append(card);
  }
  container.append(grid);
}
function addSeat(parent, slot, ids, capacity, available, pair) {
  const testid="slot-"+ids.join("+")+"-"+slot.starts_at_local.slice(11,16);
  const button=node("button",{"data-testid":testid,"data-available":String(available),type:"button",class:"seat"+(pair?" pair":"")});
  button.append(node("strong",{},seatingText(result.restaurant,ids)),node("small",{},available?(pair?"Together · ":"")+"Up to "+capacity+" guests":"Unavailable"));
  button.disabled=!available;
  if (selection && selection.restaurant.id===result.restaurant.id && selection.slot.starts_at_local===slot.starts_at_local && JSON.stringify(selection.ids)===JSON.stringify(ids)) button.classList.add("selected");
  button.onclick=()=>{
    if(!user) {feedback($("#search-form"),"auth-error","Please sign in before booking your table."); $('[data-testid="auth-error"]').scrollIntoView({block:"center"}); return;}
    // Reselecting the same option changes no booking values: retain the live
    // form, edited party size, feedback, in-flight selection and receipt identity.
    if (selection && selection.restaurant.id===result.restaurant.id &&
        selection.slot.starts_at_local===slot.starts_at_local &&
        JSON.stringify(selection.ids)===JSON.stringify(ids) && $("#booking-form")) {
      if(innerWidth<=620) $("#booking-form").scrollIntoView({block:"start"});
      return;
    }
    selection={restaurant:result.restaurant,slot,ids:[...ids],party:result.q.party_size}; pending=null;
    document.querySelectorAll(".seat.selected").forEach(el=>el.classList.remove("selected")); button.classList.add("selected");
    bookingForm();
    if(innerWidth<=620) requestAnimationFrame(()=>$("#booking-form").scrollIntoView({block:"start"}));
  };
  parent.append(button);
}
function bookingForm() {
  const area=$("#booking-area"); area.replaceChildren();
  const form=node("form",{"data-testid":"booking-form",class:"booking-panel",id:"booking-form",novalidate:""});
  form.append(node("p",{class:"eyebrow"},"Your evening"),node("h2",{},"Save your seat"));
  form.append(node("div",{"data-testid":"booking-summary",class:"booking-summary"},selection.restaurant.name+" · "+seatingText(selection.restaurant,selection.ids)+" · "+selection.slot.starts_at_local.replace("T"," at ")));
  const partyInput=field(form,"How many guests?","booking-party-size","number",selection.party);
  let previousParty=partyInput.value;
  partyInput.addEventListener("input",()=>{
    if(partyInput.value!==previousParty) pending=null;
    previousParty=partyInput.value;
  });
  form.append(node("button",{"data-testid":"booking-submit",class:"primary",type:"submit"},"Confirm reservation"));
  form.append(node("p",{class:"subtle",style:"margin:12px 0 0"},"Your time is local to the restaurant. Confirmation is instant."));
  form.onsubmit=submitBooking; area.append(form);
}
async function submitBooking(event) {
  event.preventDefault();
  if(submitting || !selection) return;
  const form=event.currentTarget, chosen=selection;
  clearFeedback(form,"booking-error"); clearFeedback(form,"booking-uncertain");
  form.querySelector('[data-testid="confirmation"]')?.remove();
  if(!user) {feedback(form,"booking-error","Please sign in before booking.");return;}
  const size=$("#booking-party-size").value;
  const body={restaurant_id:chosen.restaurant.id,
    ...(chosen.ids.length===1?{table_id:chosen.ids[0]}:{table_ids:[...chosen.ids]}),
    starts_at_local:chosen.slot.starts_at_local,party_size:Number(size)};
  const fingerprint=JSON.stringify(body)+"|"+user.user_id;
  if(!pending || pending.fingerprint!==fingerprint) pending={fingerprint,key:identity(),body};
  const retry=pending;
  const button=form.querySelector('[data-testid="booking-submit"]'); submitting=true;button.disabled=true;button.textContent="Saving your table…";
  try {
    const booking=await api("/reservations",{method:"POST",key:retry.key,body:retry.body});
    if(selection!==chosen || !form.isConnected) return;
    clearFeedback(form,"booking-uncertain"); clearFeedback(form,"booking-error");
    showConfirmation(form,booking,chosen.restaurant);
    button.textContent="Confirm again";
  } catch(error) {
    if(selection!==chosen || !form.isConnected) return;
    if(error.confirmed) {
      feedback(form,"booking-error",error.message);
      if(error.code==="table_unavailable" && result && result.q.restaurant_id===chosen.restaurant.id && result.q.date===chosen.slot.starts_at_local.slice(0,10)) await loadSearch({...result.q},true);
    } else {
      feedback(form,"booking-uncertain","We didn’t receive a confirmation. Your reservation may have been saved. Retry without changing the details so we can safely recover the same reservation.","uncertain");
    }
    button.textContent="Retry reservation";
  } finally {
    submitting=false;button.disabled=false;
  }
}
function showConfirmation(parent, booking, restaurant) {
  const ids=booking.table_ids||[booking.table_id];
  const section=node("section",{"data-testid":"confirmation",class:"confirmation"});
  section.append(node("p",{class:"success feedback"},"You’re all set. We’ll see you there."),node("h3",{},"Your reservation"));
  section.append(node("span",{class:"subtle"},"Confirmation reference"),node("span",{"data-testid":"confirmation-reference",class:"reference"},booking.reference));
  section.append(node("p",{"data-testid":"confirmation-details"},restaurant.name+" · "+seatingText(restaurant,ids)+" · "+booking.starts_at_local.replace("T"," at ")));
  section.append(node("p",{"data-testid":"confirmation-tables",class:"subtle"},seatingText(restaurant,ids)));
  const link=node("a",{href:"/lookup?reference="+encodeURIComponent(booking.reference)},"View or cancel your reservation");section.append(link);parent.append(section);
}
function lookupScreen() {
  main.innerHTML='<section class="lookup-wrap"><p class="eyebrow">Your plans, all in one place</p><h1>See you soon.</h1><p class="subtle">Use your confirmation reference to find or cancel your reservation.</p><form class="form-card" id="lookup-form" novalidate></form><div id="lookup-result"></div></section>';
  const form=$("#lookup-form");
  const input=field(form,"Confirmation reference","lookup-reference-input","text",new URLSearchParams(location.search).get("reference")||"");
  input.autocomplete="off";
  form.append(node("button",{"data-testid":"lookup-submit",type:"submit",class:"primary"},"Find reservation"));
  let epoch=0;
  form.onsubmit=async e=>{
    e.preventDefault(); const current=++epoch;const area=$("#lookup-result");area.replaceChildren();
    const button=form.querySelector("button");button.disabled=true;
    try {
      const reservation=await api("/reservations/"+encodeURIComponent(input.value.trim()));
      const restaurant=await api("/restaurants/"+encodeURIComponent(reservation.restaurant_id));
      if(current!==epoch) return;
      showDetail(area,reservation,restaurant);
    } catch(error) { if(current===epoch) feedback(area,"reservation-error",error.confirmed?error.message:"We couldn’t connect. Try your reference again."); }
    finally { if(current===epoch)button.disabled=false; }
  };
}
function showDetail(area,reservation,restaurant) {
  area.replaceChildren();
  const detail=node("section",{"data-testid":"reservation-detail",class:"detail"});
  detail.append(node("h2",{},restaurant.name),node("span",{"data-testid":"reservation-status",class:"status "+reservation.status},reservation.status));
  const list=node("dl");
  for(const [label,value] of [["Reference",reservation.reference],["Date & time",reservation.starts_at_local.replace("T"," at ")],["Guests",String(reservation.party_size)]]) {
    list.append(node("dt",{},label),node("dd",{},value));
  }
  list.append(node("dt",{},"Your tables"),node("dd",{"data-testid":"reservation-tables"},seatingText(restaurant,reservation.table_ids||[reservation.table_id])));
  detail.append(list);
  if(reservation.status==="confirmed") {
    const button=node("button",{"data-testid":"reservation-cancel-button",class:"secondary",type:"button"},"Cancel reservation");
    button.onclick=async()=>{
      button.disabled=true;clearFeedback(area,"reservation-error");
      try { const cancelled=await api("/reservations/"+encodeURIComponent(reservation.reference)+"/cancel",{method:"POST",body:{}});showDetail(area,cancelled,restaurant); }
      catch(error){feedback(area,"reservation-error",error.confirmed?error.message:"Cancellation wasn’t confirmed. Try again to check the current status.");button.disabled=false;}
    };
    detail.append(button);
  }
  area.append(detail);
}
account();
if(location.pathname==="/signup") authScreen(true);
else if(location.pathname==="/login") authScreen(false);
else if(location.pathname==="/lookup") lookupScreen();
else searchScreen();
