Non-causal orientation finding
==============================

Process model
-------------

We are using a quaternion-based Extended Kalman Filter to track body
orientation and angular rate in 3D.

From (Marins, Yun, Bachmann, McGhee, and Zyda, 2001) we have state
:math:`\boldsymbol{{\mathrm x}}=x_1,x_2,x_3,x_4,x_5,x_6,x_7` where
:math:`x_1,x_2,x_3` are angular rates :math:`p,q,r` and
:math:`x_4,x_5,x_6,x_y` are quaternion components :math:`a,b,c,d`
(with the scalar component being :math:`d`).

The temporal derivative of :math:`\boldsymbol{{\mathrm x}}` is
:math:`\dot{\boldsymbol{{\mathrm x}}}=f(\boldsymbol{{\mathrm x}})` and
is defined as:

.. math::

  \left(\begin{smallmatrix}- \frac{x_{1}}{\tau_{rx}} & - \frac{x_{2}}{\tau_{ry}} & - \frac{x_{3}}{\tau_{rz}} & \frac{x_{1} x_{7} + x_{3} x_{5} - x_{2} x_{6}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{1} x_{6} + x_{2} x_{7} - x_{3} x_{4}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{2} x_{4} + x_{3} x_{7} - x_{1} x_{5}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{3} x_{6} - x_{1} x_{4} - x_{2} x_{5}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}}\end{smallmatrix}\right)

Thus, the process update equation is:

.. math::

  \boldsymbol{{\mathrm x}}_{t+1} = \boldsymbol{{\mathrm x}}_t + 
                                   f(\boldsymbol{{\mathrm x}}_t) \delta t + 
                                   \boldsymbol{{\mathrm w}}_t

Where :math:`\boldsymbol{{\mathrm w}}_t` is the noise term with
covariance :math:`Q`.

The process update equation (for :math:`\boldsymbol{{\mathrm x}}_t \vert \boldsymbol{{\mathrm x}}_{t-1}`) is:

Using sympy__ to find the Jacobian with respect to
:math:`x_1,x_2,x_3,x_4,x_5,x_6,x_7`, we get:

__ http://sympy.org

.. math::

  \left(\begin{smallmatrix}- \frac{1}{\tau_{rx}} & 0 & 0 & 0 & 0 & 0 & 0\\0 & - \frac{1}{\tau_{ry}} & 0 & 0 & 0 & 0 & 0\\0 & 0 & - \frac{1}{\tau_{rz}} & 0 & 0 & 0 & 0\\\frac{x_{7}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{6}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{5}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{4} \left(x_{1} x_{7} + x_{3} x_{5} - x_{2} x_{6}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{5} \left(x_{1} x_{7} + x_{3} x_{5} - x_{2} x_{6}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{3}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{6} \left(x_{1} x_{7} + x_{3} x_{5} - x_{2} x_{6}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} - \frac{x_{2}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{7} \left(x_{1} x_{7} + x_{3} x_{5} - x_{2} x_{6}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{1}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}}\\\frac{x_{6}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{7}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{4}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{4} \left(x_{1} x_{6} + x_{2} x_{7} - x_{3} x_{4}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} - \frac{x_{3}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{5} \left(x_{1} x_{6} + x_{2} x_{7} - x_{3} x_{4}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{6} \left(x_{1} x_{6} + x_{2} x_{7} - x_{3} x_{4}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{1}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{7} \left(x_{1} x_{6} + x_{2} x_{7} - x_{3} x_{4}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{2}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}}\\- \frac{x_{5}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{4}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{7}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{4} \left(x_{2} x_{4} + x_{3} x_{7} - x_{1} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{2}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{5} \left(x_{2} x_{4} + x_{3} x_{7} - x_{1} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} - \frac{x_{1}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{6} \left(x_{2} x_{4} + x_{3} x_{7} - x_{1} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{7} \left(x_{2} x_{4} + x_{3} x_{7} - x_{1} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{3}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}}\\- \frac{x_{4}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{5}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & \frac{x_{6}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{4} \left(x_{3} x_{6} - x_{1} x_{4} - x_{2} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} - \frac{x_{1}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{5} \left(x_{3} x_{6} - x_{1} x_{4} - x_{2} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} - \frac{x_{2}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{6} \left(x_{3} x_{6} - x_{1} x_{4} - x_{2} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} + \frac{x_{3}}{2 \sqrt{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}} & - \frac{x_{7} \left(x_{3} x_{6} - x_{1} x_{4} - x_{2} x_{5}\right)}{2 \sqrt[3]{{x_{4}}^{2} + {x_{5}}^{2} + {x_{6}}^{2} + {x_{7}}^{2}}}\end{smallmatrix}\right)

Observation model
-----------------

The goal is to model how the target orientation given by quaternion
:math:`q=a i+b j + c k + d` results in a line on the image, and
finally, the angle of that line on the image. We also need to know the
target 3D location, the vector :math:`A`, and the camera matrix
:math:`P`. Thus, the goal is to define the function
:math:`f(q,A,P)=\theta`.

Quaternion :math:`q` may be used to rotate the vector :math:`u` using
the matrix R:

.. math::

  R = \left(\begin{smallmatrix}{a}^{2} + {d}^{2} - {b}^{2} - {c}^{2} & - 2 c d + 2 a b & 2 a c + 2 b d\\2 a b + 2 c d & {b}^{2} + {d}^{2} - {a}^{2} - {c}^{2} & - 2 a d + 2 b c\\- 2 b d + 2 a c & 2 a d + 2 b c & {c}^{2} + {d}^{2} - {a}^{2} - {b}^{2}\end{smallmatrix}\right)

Thus, for :math:`u=(1,0,0)`, we find :math:`U=Ru`, the orientation
estimate.

.. math::

  U=Ru = \left(\begin{smallmatrix}{a}^{2} + {d}^{2} - {b}^{2} - {c}^{2}\\2 a b + 2 c d\\- 2 b d + 2 a c\end{smallmatrix}\right)

Now, considering a point passing through :math:`A` with orientation
given by :math:`U`, we define a second point :math:`B=A+U`.

Given the camera matrix :math:`P`:

.. math::

  P = \left(\begin{smallmatrix}P_{00} & P_{01} & P_{02} & P_{03}\\P_{10} & P_{11} & P_{12} & P_{13}\\P_{20} & P_{21} & P_{22} & P_{23}\end{smallmatrix}\right)

The image of point :math:`A` is :math:`a=PA`.

.. math::

  \theta = \operatorname{atan}\left(\frac{\frac{P_{13} + Ax P_{10} + Ay P_{11} + Az P_{12} - 2 P_{12} b d + 2 P_{11} a b + 2 P_{11} c d + 2 P_{12} a c + P_{10} {a}^{2} + P_{10} {d}^{2} - P_{10} {b}^{2} - P_{10} {c}^{2}}{P_{23} + Ax P_{20} + Ay P_{21} + Az P_{22} - 2 P_{22} b d + 2 P_{21} a b + 2 P_{21} c d + 2 P_{22} a c + P_{20} {a}^{2} + P_{20} {d}^{2} - P_{20} {b}^{2} - P_{20} {c}^{2}} - \frac{P_{13} + Ax P_{10} + Ay P_{11} + Az P_{12}}{P_{23} + Ax P_{20} + Ay P_{21} + Az P_{22}}}{\frac{P_{03} + Ax P_{00} + Ay P_{01} + Az P_{02} - 2 P_{02} b d + 2 P_{01} a b + 2 P_{01} c d + 2 P_{02} a c + P_{00} {a}^{2} + P_{00} {d}^{2} - P_{00} {b}^{2} - P_{00} {c}^{2}}{P_{23} + Ax P_{20} + Ay P_{21} + Az P_{22} - 2 P_{22} b d + 2 P_{21} a b + 2 P_{21} c d + 2 P_{22} a c + P_{20} {a}^{2} + P_{20} {d}^{2} - P_{20} {b}^{2} - P_{20} {c}^{2}} - \frac{P_{03} + Ax P_{00} + Ay P_{01} + Az P_{02}}{P_{23} + Ax P_{20} + Ay P_{21} + Az P_{22}}}\right)
