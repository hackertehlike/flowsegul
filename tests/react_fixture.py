"""A tiny React + TypeScript checkout app, for flowsegul's React mode.

Used by tests/test_react.py and handy for trying the page by hand:
    python3 tests/react_fixture.py /tmp/shopweb && flowsegul --repo /tmp/shopweb --react

The checkout: CheckoutPage owns `couponCode` and hands `setCouponCode` down as `onApply`, through
PromoPanel to CouponField. CartSummary passes the code on to PriceLine, whose effect fetches a
preview price and stores it with `.then(setTotal)`. The cart comes from a hook and is shared through
a context. A few edge cases ride along: a setter wrapped in useCallback, a memo child that gets no
props, `props.x` access, a default export, an index.ts re-export, and props passed on with a spread.
"""
import os, subprocess, sys, textwrap

FILES = {
    'package.json': '''
        {"name": "shopweb", "private": true, "dependencies": {"react": "^18.2.0"}}
    ''',
    'tsconfig.json': '''
        {"compilerOptions": {"jsx": "react-jsx", "strict": true, "module": "esnext",
                             "moduleResolution": "bundler", "target": "es2020"},
         "include": ["src"]}
    ''',
    'src/App.tsx': '''
        import { CheckoutPage } from "./checkout";
        import { ThemeSwitch } from "./settings/ThemeSwitch";

        export default function App() {
          return (
            <main>
              <CheckoutPage />
              <ThemeSwitch />
            </main>
          );
        }
    ''',
    'src/checkout/index.ts': '''
        export { CheckoutPage } from "./CheckoutPage";
    ''',
    'src/checkout/CartContext.ts': '''
        import { createContext } from "react";

        export const CartContext = createContext<{ items: string[]; setItems: (items: string[]) => void }>({
          items: [],
          setItems: () => {},
        });
    ''',
    'src/checkout/useCart.ts': '''
        import { useEffect, useState } from "react";

        export function useCart() {
          const [items, setItems] = useState<string[]>([]);
          useEffect(() => {
            fetch("/cart").then((r) => r.json()).then(setItems);
          }, []);
          return { items, setItems };
        }
    ''',
    'src/checkout/CheckoutPage.tsx': '''
        import { useState } from "react";
        import { CartContext } from "./CartContext";
        import { useCart } from "./useCart";
        import CartSummary from "./CartSummary";
        import { PromoPanel } from "./PromoPanel";
        import { NoteBox } from "./NoteBox";
        import { Footer } from "./Footer";

        export function CheckoutPage() {
          const cart = useCart();
          const [couponCode, setCouponCode] = useState("");
          return (
            <CartContext.Provider value={cart}>
              <CartSummary couponCode={couponCode} />
              <PromoPanel onApply={setCouponCode} />
              <NoteBox />
              <Footer />
            </CartContext.Provider>
          );
        }
    ''',
    'src/checkout/CartSummary.tsx': '''
        import { useContext } from "react";
        import { CartContext } from "./CartContext";
        import { PriceLine } from "./PriceLine";

        export default function CartSummary(props: { couponCode: string }) {
          const { setItems } = useContext(CartContext);
          const discountCents = 100;
          return (
            <div>
              <PriceLine couponCode={props.couponCode} discountCents={discountCents} />
              <button onClick={() => setItems([])}>Clear cart</button>
            </div>
          );
        }
    ''',
    'src/checkout/PriceLine.tsx': '''
        import { useContext, useEffect, useState } from "react";
        import { CartContext } from "./CartContext";

        export function PriceLine({ couponCode, discountCents }: { couponCode: string; discountCents: number }) {
          const { items } = useContext(CartContext);
          const [total, setTotal] = useState<number>();
          useEffect(() => {
            fetch(`/orders/preview?code=${couponCode}&n=${items.length}`)
              .then((r) => r.json())
              .then(setTotal);
          }, [couponCode, items]);
          return <span>{(total ?? 0) - discountCents}</span>;
        }
    ''',
    'src/checkout/PromoPanel.tsx': '''
        import { CouponField } from "./CouponField";

        export function PromoPanel({ onApply }: { onApply: (code: string) => void }) {
          return (
            <section title="Promo">
              <CouponField onApply={onApply} />
              <button onClick={() => onApply("")}>Remove</button>
            </section>
          );
        }
    ''',
    'src/checkout/CouponField.tsx': '''
        import { useState } from "react";

        export function CouponField({ onApply }: { onApply: (code: string) => void }) {
          const [draft, setDraft] = useState("");
          return (
            <div>
              <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="coupon" />
              <button onClick={() => onApply(draft)}>Apply</button>
            </div>
          );
        }
    ''',
    # a setter behind useCallback, and one behind a plain arrow
    'src/checkout/NoteBox.tsx': '''
        import { useCallback, useState } from "react";

        export function NoteBox() {
          const [note, setNote] = useState("");
          const handleNote = useCallback((v: string) => setNote(v), []);
          const clear = () => setNote("");
          return (
            <div>
              <textarea value={note} onChange={(e) => handleNote(e.target.value)} aria-label="Note" />
              <button onClick={clear}>Clear note</button>
            </div>
          );
        }
    ''',
    # a memo child that gets no props: it does not rerun when the page's state changes
    'src/checkout/Footer.tsx': '''
        import { memo } from "react";

        export const Footer = memo(function Footer() {
          return <footer>Secure checkout</footer>;
        });
    ''',
    # props passed on with a spread: flowsegul can't be sure what goes through
    'src/settings/ThemeSwitch.tsx': '''
        import { useState } from "react";

        export function ThemeSwitch() {
          const [theme, setTheme] = useState("light");
          return <ThemeButtons onPick={setTheme} current={theme} />;
        }

        function ThemeButtons(props: { onPick: (t: string) => void; current: string }) {
          return <ThemeButton {...props} />;
        }

        function ThemeButton({ onPick }: { onPick: (t: string) => void }) {
          return <button onClick={() => onPick("dark")}>Dark</button>;
        }
    ''',
}


def make(root, git=True):
    for rel, body in FILES.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(body).lstrip('\n'))
    if git and not os.path.isdir(os.path.join(root, '.git')):
        run = lambda *a: subprocess.run(['git', '-C', root, *a], check=True, capture_output=True)
        run('init', '-q', '-b', 'main')
        run('config', 'user.email', 't@example.com'); run('config', 'user.name', 't')
        run('add', '-A'); run('commit', '-qm', 'checkout')
    return root


if __name__ == '__main__':
    print(make(sys.argv[1]))
